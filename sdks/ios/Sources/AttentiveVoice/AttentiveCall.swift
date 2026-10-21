import AVFoundation
import Combine
import Foundation

@MainActor
protocol CallTransport: AnyObject {
    var onEvent: ((CallEvent) -> Void)? { get set }
    var microphoneEnabled: Bool? { get }
    func connect(_ credentials: CallCredentials, microphoneEnabled: Bool) async throws
    func disconnect() async
    func setMicrophone(enabled: Bool) async throws
    func sendText(_ text: String) async throws -> String
}

extension CallTransport {
    var microphoneEnabled: Bool? { nil }
}

/// One active call per instance. Retain it while the call is active, then call end().
@MainActor
public final class AttentiveCall: ObservableObject {
    @Published public private(set) var state: CallState = .idle
    @Published public private(set) var agentState: AgentState = .waiting
    @Published public private(set) var microphoneEnabled = false
    @Published public private(set) var sessionAuthentication: AuthenticationStatus.Status = .pending
    @Published public private(set) var actionAuthentication: AuthenticationStatus.Status = .pending
    @Published public private(set) var transcripts: [Transcript] = []
    @Published public private(set) var toolActivity: [ToolActivity] = []
    @Published public private(set) var lastError: CallError?
    @Published public private(set) var configuration: AgentConfiguration?
    public let agentAudioLevel = AgentAudioLevel()

    /// Called on the main actor. Treat payloads as private caller data.
    public var onEvent: ((CallEvent) -> Void)?

    private let provider: (any CallCredentialProvider)?
    private var sdkClient: SDKClient?
    private let allowInsecure: Bool
    private let makeTransport: () -> any CallTransport
    private let microphonePermission: () async -> Bool
    private let agentTimeout: Duration
    private var transport: (any CallTransport)?
    private var attempt: UUID?
    private var operation: Task<Void, Error>?
    private var watchdog: Task<Void, Never>?
    private var changingMicrophone = false

    /// Uses your dashboard calling key and agent. No endpoint or transport setup is required.
    public convenience init(apiKey: String, agentID: String) {
        self.init(apiKey: apiKey, agentID: agentID, customerID: nil)
    }

    /// Customer selection must match the identity in a backend-issued caller token.
    public convenience init(apiKey: String, agentID: String, customerID: String?) {
        self.init(credentialProvider: nil, allowsInsecureDevelopmentConnections: false)
        sdkClient = SDKClient(apiKey: apiKey, agentID: agentID, customerID: customerID)
    }

    /// Refresh dashboard display settings without requesting microphone access or starting a call.
    public func loadConfiguration() async throws {
        guard let sdkClient else { throw CallError.invalidRequest }
        let value = try await sdkClient.configuration()
        try Task.checkCancellation()
        configuration = value
    }

    /// callerToken is optional for general calls. Only your backend can issue authenticated caller context.
    public func start(callerToken: String? = nil, microphoneEnabled: Bool = true) async throws {
        guard let sdkClient else { throw CallError.invalidRequest }
        try sdkClient.validate()
        try await connect(microphoneEnabled: microphoneEnabled) {
            try await sdkClient.credentials(callerToken: callerToken)
        }
    }

    public convenience init(endpoint: URL, allowsInsecureDevelopmentConnections: Bool = false) throws {
        let provider = try HTTPCallCredentialProvider(endpoint: endpoint,
            allowsInsecureDevelopmentConnections: allowsInsecureDevelopmentConnections)
        self.init(credentialProvider: provider, allowsInsecureDevelopmentConnections: allowsInsecureDevelopmentConnections)
    }

    public convenience init(credentialProvider: any CallCredentialProvider,
                            allowsInsecureDevelopmentConnections: Bool = false) {
        self.init(credentialProvider: Optional(credentialProvider),
                  allowsInsecureDevelopmentConnections: allowsInsecureDevelopmentConnections)
    }

    private convenience init(credentialProvider: (any CallCredentialProvider)?,
                             allowsInsecureDevelopmentConnections: Bool) {
        self.init(provider: credentialProvider, allowInsecure: allowsInsecureDevelopmentConnections,
                  makeTransport: { RealtimeCallTransport() }, microphonePermission: {
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized: return true
            case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
            default: return false
            }
        })
    }

    init(provider: (any CallCredentialProvider)?, allowInsecure: Bool = false,
         makeTransport: @escaping () -> any CallTransport,
         microphonePermission: @escaping () async -> Bool, agentTimeout: Duration = .seconds(30)) {
        self.provider = provider
        self.allowInsecure = allowInsecure
        self.makeTransport = makeTransport
        self.microphonePermission = microphonePermission
        self.agentTimeout = agentTimeout
    }

    /// Returns after joining and publishing audio, not after the agent's greeting.
    /// microphoneEnabled=false is useful for chat-only diagnostics, not voice auth.
    public func start(_ request: CallRequest, microphoneEnabled: Bool = true) async throws {
        guard operation == nil, [.idle, .ended, .failed].contains(state) else { throw CallError.alreadyActive }
        try request.validate()
        guard let provider else { throw CallError.invalidRequest }
        try await connect(microphoneEnabled: microphoneEnabled) { try await provider.fetch(for: request) }
    }

    private func connect(microphoneEnabled: Bool, credentials: @escaping () async throws -> CallCredentials) async throws {
        guard operation == nil, [.idle, .ended, .failed].contains(state) else { throw CallError.alreadyActive }
        transcripts = []
        toolActivity = []
        lastError = nil
        changingMicrophone = false
        sessionAuthentication = .pending
        actionAuthentication = .pending
        agentState = .waiting
        agentAudioLevel.reset()
        let id = UUID()
        attempt = id
        let connection = makeTransport()
        transport = connection
        connection.onEvent = { [weak self] event in
            guard let self, self.attempt == id else { return }
            self.receive(event)
        }
        setState(.connecting)
        let task = Task { [self] in
            do {
                if microphoneEnabled, !(await microphonePermission()) { throw CallError.microphoneDenied }
                try Task.checkCancellation()
                let credentials = try await credentials()
                try Task.checkCancellation()
                guard !credentials.participantToken.isEmpty, !credentials.roomName.isEmpty else { throw CallError.invalidResponse }
                try validateURL(credentials.serverUrl, schemes: ["wss", "ws"], allowInsecure: allowInsecure)
                try await connection.connect(credentials, microphoneEnabled: microphoneEnabled)
                try Task.checkCancellation()
                guard attempt == id else { throw CancellationError() }
                self.microphoneEnabled = connection.microphoneEnabled ?? microphoneEnabled
                setState(.connected)
                onEvent?(.microphoneChanged(self.microphoneEnabled))
                watchForAgent(id: id)
            } catch {
                connection.onEvent = nil
                await connection.disconnect()
                if attempt == id {
                    attempt = nil
                    transport = nil
                    self.microphoneEnabled = false
                    lastError = error as? CallError ?? .connectionFailed
                    setState(.failed)
                    onEvent?(.failure(lastError!))
                }
                if Task.isCancelled { throw CancellationError() }
                throw error as? CallError ?? CallError.connectionFailed
            }
        }
        operation = task
        defer { if attempt == id || attempt == nil { operation = nil } }
        try await withTaskCancellationHandler(operation: { try await task.value }, onCancel: { task.cancel() })
    }

    public func end() async {
        guard state != .ending else { return }
        attempt = nil
        watchdog?.cancel()
        watchdog = nil
        let pending = operation
        pending?.cancel()
        let connection = transport
        connection?.onEvent = nil
        setState(.ending)
        await connection?.disconnect()
        _ = try? await pending?.value
        operation = nil
        transport = nil
        changingMicrophone = false
        microphoneEnabled = false
        agentState = .disconnected
        sessionAuthentication = .pending
        actionAuthentication = .pending
        setState(.ended)
    }

    public func setMicrophone(enabled: Bool) async throws {
        guard state == .connected, let connection = transport, let id = attempt else { throw CallError.notConnected }
        guard !changingMicrophone else { throw CallError.alreadyActive }
        changingMicrophone = true
        defer { if attempt == id { changingMicrophone = false } }
        if enabled, !(await microphonePermission()) { throw CallError.microphoneDenied }
        guard attempt == id else { throw CallError.notConnected }
        do { try await connection.setMicrophone(enabled: enabled) }
        catch { throw CallError.connectionFailed }
        guard attempt == id else { throw CallError.notConnected }
        microphoneEnabled = connection.microphoneEnabled ?? enabled
        onEvent?(.microphoneChanged(microphoneEnabled))
    }

    public func sendText(_ text: String) async throws {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.utf8.count <= 16_384 else { throw CallError.invalidRequest }
        guard state == .connected, agentState != .waiting, agentState != .disconnected,
              let connection = transport, let id = attempt else { throw CallError.notConnected }
        do {
            let messageID = try await connection.sendText(text)
            guard attempt == id else { throw CallError.notConnected }
            receive(.transcript(.init(id: "caller:\(messageID)", speaker: .caller, text: text, isFinal: true)))
        } catch { throw error as? CallError ?? CallError.messageFailed }
    }

    private func setState(_ value: CallState) {
        if value != .connected && value != .connecting { agentAudioLevel.reset() }
        state = value
        onEvent?(.stateChanged(value))
    }

    private func watchForAgent(id: UUID) {
        guard agentState == .waiting || agentState == .initializing else { return }
        watchdog?.cancel()
        watchdog = Task { [weak self, agentTimeout] in
            do { try await Task.sleep(for: agentTimeout) } catch { return }
            guard let self, self.attempt == id,
                  self.agentState == .waiting || self.agentState == .initializing else { return }
            await self.fail(.agentUnavailable)
        }
    }

    private func fail(_ error: CallError) async {
        guard state != .ending else { return }
        attempt = nil
        watchdog?.cancel()
        watchdog = nil
        let pending = operation
        pending?.cancel()
        let connection = transport
        connection?.onEvent = nil
        setState(.ending)
        await connection?.disconnect()
        _ = try? await pending?.value
        operation = nil
        transport = nil
        microphoneEnabled = false
        agentState = .disconnected
        sessionAuthentication = .pending
        actionAuthentication = .pending
        lastError = error
        changingMicrophone = false
        setState(.failed)
        onEvent?(.failure(error))
    }

    private func receive(_ event: CallEvent) {
        switch event {
        case .microphoneChanged(let enabled):
            microphoneEnabled = enabled
        case .stateChanged(let value):
            if value == .failed {
                let id = attempt
                Task { [weak self] in
                    guard let self, self.attempt == id else { return }
                    await self.fail(.connectionFailed)
                }
                return
            }
            // Initial readiness is set only after microphone publishing succeeds.
            guard state != .connecting else { return }
            state = value
            if value != .connected { agentAudioLevel.reset() }
        case .agentStateChanged(let value):
            agentState = value
            if value != .speaking { agentAudioLevel.reset() }
            if [.listening, .thinking, .speaking].contains(value) { watchdog?.cancel() }
            if value == .disconnected {
                let id = attempt
                Task { [weak self] in
                    guard let self, self.attempt == id else { return }
                    await self.fail(.agentUnavailable)
                }
            }
        case .agentAudioEnergy(let value):
            guard state == .connected || state == .connecting else { return }
            agentAudioLevel.update(value)
        case .transcript(let transcript):
            if let index = transcripts.firstIndex(where: { $0.id == transcript.id }) {
                guard !transcripts[index].isFinal || transcript.isFinal else { return }
                if transcripts[index] == transcript { return }
                transcripts[index] = transcript
            } else { transcripts.append(transcript) }
            if transcripts.count > 500 { transcripts.removeFirst(transcripts.count - 500) }
        case .toolActivity(let activity):
            if let index = toolActivity.firstIndex(where: { $0.id == activity.id }) { toolActivity[index] = activity }
            else { toolActivity.append(activity) }
            if toolActivity.count > 100 { toolActivity.removeFirst(toolActivity.count - 100) }
        case .authentication(let auth):
            if auth.scope == .session { sessionAuthentication = auth.status }
            else { actionAuthentication = auth.status }
        default: break
        }
        onEvent?(event)
    }
}
