import AVFoundation
import Combine
import Foundation
#if os(iOS)
import UIKit
#endif

@MainActor
protocol CallTransport: AnyObject {
    var onEvent: ((CallEvent) -> Void)? { get set }
    var microphoneEnabled: Bool? { get }
    func connect(_ credentials: CallCredentials, microphoneEnabled: Bool) async throws
    func disconnect() async
    func setMicrophone(enabled: Bool) async throws
    func sendText(_ text: String) async throws -> String
    var onPasskeyEvent: ((PasskeyTransportEvent) -> Void)? { get set }
    func setPasskeysEnabled(_ enabled: Bool)
    func setPasskeyDiagnostics(_ diagnostics: PasskeyDiagnostics)
    func sendPasskeyMessage(_ data: Data) async throws
    func setAudioInputConfiguration(_ configuration: AttentiveAudioInputConfiguration) async throws
}

extension CallTransport {
    var onPasskeyEvent: ((PasskeyTransportEvent) -> Void)? { get { nil } set {} }
    func setPasskeysEnabled(_ enabled: Bool) {}
    func setPasskeyDiagnostics(_ diagnostics: PasskeyDiagnostics) {}
    func sendPasskeyMessage(_ data: Data) async throws { throw PasskeyFailure.unavailable }
    var microphoneEnabled: Bool? { nil }
    func setAudioInputConfiguration(_ configuration: AttentiveAudioInputConfiguration) async throws {
        throw AttentiveAudioInputError.unsupported
    }
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
    @Published public private(set) var audioInputConfiguration: AttentiveAudioInputConfiguration
    @Published public private(set) var audioInputState: AttentiveAudioInputState
    public let agentAudioLevel = AgentAudioLevel()
    public let passkeyConfiguration: AttentivePasskeyConfiguration
    private lazy var passkeyDiagnostics = PasskeyDiagnostics(enabled: passkeyConfiguration.diagnosticsEnabled)
    // Internal dependency injection for interactive tests; never a customer API.
    private let passkeyAuthorizerOverride: (any PasskeyAuthorizing)?
    #if os(iOS)
    /// Headless hosts return the window presenting this call. AttentiveAgentView supplies it automatically.
    public var passkeyPresentationAnchor: (() -> UIWindow?)?
    #endif
    private lazy var passkeyCoordinator: PasskeyCoordinator = {
        let authorizer: any PasskeyAuthorizing
        if let passkeyAuthorizerOverride {
            authorizer = passkeyAuthorizerOverride
        } else {
            #if os(iOS)
            authorizer = NativePasskeyAuthorizer(diagnostics: passkeyDiagnostics) { [weak self] in self?.passkeyPresentationAnchor?() }
            #else
            authorizer = UnavailablePasskeyAuthorizer()
            #endif
        }
        return PasskeyCoordinator(configuration: passkeyConfiguration, authorizer: authorizer, diagnostics: passkeyDiagnostics,
            send: { [weak self] data in
                guard let self, let transport = self.transport, self.attempt != nil else { throw PasskeyFailure.unavailable }
                try await transport.sendPasskeyMessage(data)
            }, status: { [weak self] status in
                guard let self else { return }
                self.actionAuthentication = status
                self.onEvent?(.authentication(.init(scope: .action, status: status)))
            })
    }()

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
    public convenience init(apiKey: String, agentID: String, audioInput: AttentiveAudioInputConfiguration = .standard,
                            passkeys: AttentivePasskeyConfiguration = .disabled,
                            voiceLanguage: VoiceAccent? = nil, voiceGender: VoiceGender? = nil) {
        self.init(apiKey: apiKey, agentID: agentID, customerID: nil, audioInput: audioInput, passkeys: passkeys,
                  voiceLanguage: voiceLanguage, voiceGender: voiceGender)
    }

    /// Customer selection must match the identity in a backend-issued caller token.
    public convenience init(apiKey: String, agentID: String, customerID: String?, audioInput: AttentiveAudioInputConfiguration = .standard,
                            passkeys: AttentivePasskeyConfiguration = .disabled,
                            voiceLanguage: VoiceAccent? = nil, voiceGender: VoiceGender? = nil) {
        self.init(credentialProvider: nil, allowsInsecureDevelopmentConnections: false, audioInput: audioInput, passkeys: passkeys)
        sdkClient = SDKClient(apiKey: apiKey, agentID: agentID, customerID: customerID,
                              voiceLanguage: voiceLanguage, voiceGender: voiceGender)
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

    public convenience init(endpoint: URL, allowsInsecureDevelopmentConnections: Bool = false,
                            audioInput: AttentiveAudioInputConfiguration = .standard,
                            passkeys: AttentivePasskeyConfiguration = .disabled) throws {
        let provider = try HTTPCallCredentialProvider(endpoint: endpoint,
            allowsInsecureDevelopmentConnections: allowsInsecureDevelopmentConnections)
        self.init(credentialProvider: provider, allowsInsecureDevelopmentConnections: allowsInsecureDevelopmentConnections, audioInput: audioInput, passkeys: passkeys)
    }

    public convenience init(credentialProvider: any CallCredentialProvider,
                            allowsInsecureDevelopmentConnections: Bool = false,
                            audioInput: AttentiveAudioInputConfiguration = .standard,
                            passkeys: AttentivePasskeyConfiguration = .disabled) {
        self.init(credentialProvider: Optional(credentialProvider),
                  allowsInsecureDevelopmentConnections: allowsInsecureDevelopmentConnections, audioInput: audioInput, passkeys: passkeys)
    }

    private convenience init(credentialProvider: (any CallCredentialProvider)?,
                             allowsInsecureDevelopmentConnections: Bool,
                             audioInput: AttentiveAudioInputConfiguration, passkeys: AttentivePasskeyConfiguration) {
        self.init(provider: credentialProvider, allowInsecure: allowsInsecureDevelopmentConnections,
                  makeTransport: { RealtimeCallTransport() }, microphonePermission: {
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized: return true
            case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
            default: return false
            }
        }, audioInput: audioInput, passkeys: passkeys)
    }

    init(provider: (any CallCredentialProvider)?, allowInsecure: Bool = false,
         makeTransport: @escaping () -> any CallTransport,
         microphonePermission: @escaping () async -> Bool, agentTimeout: Duration = .seconds(30),
         audioInput: AttentiveAudioInputConfiguration = .standard,
         passkeys: AttentivePasskeyConfiguration = .disabled,
         passkeyAuthorizer: (any PasskeyAuthorizing)? = nil) {
        self.provider = provider
        self.allowInsecure = allowInsecure
        self.makeTransport = makeTransport
        self.microphonePermission = microphonePermission
        self.agentTimeout = agentTimeout
        passkeyAuthorizerOverride = passkeyAuthorizer
        passkeyConfiguration = passkeys
        audioInputConfiguration = audioInput
        audioInputState = .init(status: .idle, requested: audioInput)
    }

    /// Returns after joining and publishing audio, not after the agent's greeting.
    /// Non-default listening preferences wait up to ten seconds for worker negotiation
    /// before enabling the microphone. Unsupported settings leave the call usable.
    /// microphoneEnabled=false is useful for chat-only diagnostics, not voice auth.
    public func start(_ request: CallRequest, microphoneEnabled: Bool = true) async throws {
        guard operation == nil, [.idle, .ended, .failed].contains(state) else { throw CallError.alreadyActive }
        try request.validate()
        guard let provider else { throw CallError.invalidRequest }
        try await connect(microphoneEnabled: microphoneEnabled) { try await provider.fetch(for: request) }
    }

    private func connect(microphoneEnabled: Bool, credentials: @escaping () async throws -> CallCredentials) async throws {
        guard operation == nil, [.idle, .ended, .failed].contains(state) else { throw CallError.alreadyActive }
        try audioInputConfiguration.validate()
        do {
            try passkeyConfiguration.validate()
            passkeyDiagnostics.record(.configuration, passkeyConfiguration.isEnabled ? .succeeded : .ignored)
        } catch {
            passkeyDiagnostics.record(.configuration, .failed, reason: .invalidRequest)
            throw error
        }
        passkeyCoordinator.reset()
        transcripts = []
        toolActivity = []
        lastError = nil
        changingMicrophone = false
        sessionAuthentication = .pending
        actionAuthentication = .pending
        agentState = .waiting
        agentAudioLevel.reset()
        audioInputState = .init(status: .pending, requested: audioInputConfiguration)
        let id = UUID()
        attempt = id
        let connection = makeTransport()
        transport = connection
        connection.setPasskeysEnabled(passkeyConfiguration.isEnabled)
        connection.setPasskeyDiagnostics(passkeyDiagnostics)
        connection.onPasskeyEvent = { [weak self] event in
            guard let self, self.attempt == id else { return }
            self.passkeyCoordinator.receive(event)
        }
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
                // Store preferences before connecting. Capability negotiation never prevents a call.
                try? await connection.setAudioInputConfiguration(audioInputConfiguration)
                try await connection.connect(credentials, microphoneEnabled: microphoneEnabled)
                try Task.checkCancellation()
                guard attempt == id else { throw CancellationError() }
                self.microphoneEnabled = connection.microphoneEnabled ?? microphoneEnabled
                setState(.connected)
                onEvent?(.microphoneChanged(self.microphoneEnabled))
                watchForAgent(id: id)
            } catch {
                connection.onEvent = nil
                connection.onPasskeyEvent = nil
                passkeyCoordinator.suspend()
                await connection.disconnect()
                if attempt == id {
                    attempt = nil
                    transport = nil
                    self.microphoneEnabled = false
                    audioInputState = .init(status: .idle, requested: audioInputConfiguration)
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
        passkeyCoordinator.suspend()
        watchdog?.cancel()
        watchdog = nil
        let pending = operation
        pending?.cancel()
        let connection = transport
        connection?.onEvent = nil
        connection?.onPasskeyEvent = nil
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
        audioInputState = .init(status: .idle, requested: audioInputConfiguration)
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

    /// Saves preferences for the next call, or waits for a compatible connected worker's
    /// acknowledgement. Settings failures do not end the call or change microphone mute.
    /// Cancellation stops waiting; an already sent request may still be acknowledged.
    public func setAudioInputConfiguration(_ value: AttentiveAudioInputConfiguration) async throws {
        try Task.checkCancellation()
        try value.validate()
        audioInputConfiguration = value
        guard let connection = transport, let id = attempt else {
            audioInputState = .init(status: .idle, requested: value)
            onEvent?(.audioInputChanged(audioInputState))
            return
        }
        audioInputState = .init(status: .pending, requested: value, applied: audioInputState.applied)
        onEvent?(.audioInputChanged(audioInputState))
        do {
            try await connection.setAudioInputConfiguration(value)
            guard attempt == id else { throw CancellationError() }
        } catch {
            if error is CancellationError, attempt == id, audioInputConfiguration == value,
               audioInputState.status == .pending {
                // Cancellation can happen before the transport accepts this request, or
                // after sending. Neither case confirms the worker's effective settings.
                audioInputState = .init(status: .failed, requested: value, applied: audioInputState.applied,
                                       message: "Stopped waiting for listening settings. An update already sent may still be applied.")
                onEvent?(.audioInputChanged(audioInputState))
            }
            guard attempt == id, audioInputConfiguration == value,
                  !(error is CancellationError), error as? AttentiveAudioInputError != .superseded else { throw error }
            let failure = error as? AttentiveAudioInputError ?? .updateFailed
            audioInputState = .init(status: failure == .unsupported ? .unsupported : .failed,
                                   requested: value, applied: audioInputState.applied, message: failure.errorDescription)
            onEvent?(.audioInputChanged(audioInputState))
            throw failure
        }
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
        passkeyCoordinator.suspend()
        watchdog?.cancel()
        watchdog = nil
        let pending = operation
        pending?.cancel()
        let connection = transport
        connection?.onEvent = nil
        connection?.onPasskeyEvent = nil
        setState(.ending)
        await connection?.disconnect()
        _ = try? await pending?.value
        operation = nil
        transport = nil
        microphoneEnabled = false
        agentState = .disconnected
        sessionAuthentication = .pending
        actionAuthentication = .pending
        audioInputState = .init(status: .idle, requested: audioInputConfiguration)
        lastError = error
        changingMicrophone = false
        setState(.failed)
        onEvent?(.failure(error))
    }

    private func receive(_ event: CallEvent) {
        switch event {
        case .audioInputChanged(let value):
            // Only current requested settings may update UI, even if an old RPC finishes late.
            guard value.requested == audioInputConfiguration else { return }
            audioInputState = value
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
            // Legacy voice-auth observations cannot authorize a passkey-protected action.
            if passkeyConfiguration.isEnabled, auth.scope == .action { return }
            if auth.scope == .session { sessionAuthentication = auth.status }
            else { actionAuthentication = auth.status }
        default: break
        }
        onEvent?(event)
    }
}
