import Foundation

/// Listening preferences applied by a compatible agent worker before speech recognition.
/// The SDK always retains its normal microphone echo cancellation, noise suppression and
/// gain control. These settings do not change microphone permission, mute, or voice pitch.
public struct AttentiveAudioInputConfiguration: Codable, Equatable, Sendable {
    public enum NoiseControl: String, Codable, CaseIterable, Identifiable, Sendable {
        case standard, noisyEnvironment
        public var id: String { rawValue }
    }

    public enum ResponseSensitivity: String, Codable, CaseIterable, Identifiable, Sendable {
        case responsive, balanced, strict
        public var id: String { rawValue }
    }

    public var noiseControl: NoiseControl
    public var responseSensitivity: ResponseSensitivity

    public init(noiseControl: NoiseControl = .standard, responseSensitivity: ResponseSensitivity = .balanced) {
        self.noiseControl = noiseControl
        self.responseSensitivity = responseSensitivity
    }

    public static let standard = Self()
    public static let noisyEnvironment = Self(noiseControl: .noisyEnvironment)
}

/// The requested settings are not necessarily the settings acknowledged by the worker.
public struct AttentiveAudioInputState: Equatable, Sendable {
    public enum Status: String, Sendable { case idle, pending, applied, unsupported, failed }
    public let status: Status
    public let requested: AttentiveAudioInputConfiguration
    /// Last acknowledged settings for this connection. Nil means no acknowledged settings.
    public let applied: AttentiveAudioInputConfiguration?
    public let message: String?

    init(status: Status, requested: AttentiveAudioInputConfiguration,
         applied: AttentiveAudioInputConfiguration? = nil, message: String? = nil) {
        self.status = status
        self.requested = requested
        self.applied = applied
        self.message = message
    }
}

public enum AttentiveAudioInputError: Error, Equatable, LocalizedError, Sendable {
    case unsupported, timedOut, invalidAcknowledgement, updateFailed, superseded, callerSpeaking, processingUnavailable

    public var errorDescription: String? {
        switch self {
        case .unsupported: "This agent does not support listening settings. Standard microphone processing remains active."
        case .timedOut: "The agent did not confirm the listening settings. Try again."
        case .invalidAcknowledgement: "The agent returned an invalid listening settings confirmation."
        case .updateFailed: "The listening settings could not be applied. Try again."
        case .superseded: "A newer listening settings request replaced this update."
        case .callerSpeaking: "Pause speaking and try again. The previous listening settings are still active."
        case .processingUnavailable: "Listening processing is unavailable on this agent. The previous settings remain active."
        }
    }
}

/// Deliberately separate from the public configuration so its wire version cannot be changed by callers.
struct ListeningConfigurationMessage: Codable, Equatable {
    let version: Int
    let noiseControl: AttentiveAudioInputConfiguration.NoiseControl
    let responseSensitivity: AttentiveAudioInputConfiguration.ResponseSensitivity

    init(_ configuration: AttentiveAudioInputConfiguration) {
        version = 1
        noiseControl = configuration.noiseControl
        responseSensitivity = configuration.responseSensitivity
    }

    var configuration: AttentiveAudioInputConfiguration {
        .init(noiseControl: noiseControl, responseSensitivity: responseSensitivity)
    }

    static func acknowledgement(_ payload: String) throws -> AttentiveAudioInputConfiguration {
        guard payload.utf8.count <= 4_096,
              let message = try? JSONDecoder().decode(Self.self, from: Data(payload.utf8)),
              message.version == 1 else { throw AttentiveAudioInputError.invalidAcknowledgement }
        return message.configuration
    }
}

/// Serializes updates so a slow earlier RPC cannot overwrite a newer request at the worker.
/// Audio continues on its existing track throughout negotiation, including while muted.
@MainActor
final class AudioInputNegotiator {
    struct Peer: Equatable { let identity: String; let version: String? }
    var onChange: ((AttentiveAudioInputState) -> Void)?
    private(set) var state: AttentiveAudioInputState
    private var peer: Peer?
    private var connected = false
    private var closed = false
    private var revision = 0
    private var epoch = 0
    private var pump: Task<Void, Never>?
    private var discovery: Task<Void, Never>?
    private var waiters: [UUID: CheckedContinuation<Void, Error>] = [:]
    private var deadlines: [UUID: Task<Void, Never>] = [:]
    private let timeout: Duration
    private let send: (String, String) async throws -> String

    init(configuration: AttentiveAudioInputConfiguration = .standard, timeout: Duration = .seconds(10),
         send: @escaping (String, String) async throws -> String) {
        state = .init(status: .idle, requested: configuration)
        self.timeout = timeout
        self.send = send
    }

    func setConnected(_ value: Bool) {
        guard !closed, connected != value else { return }
        connected = value
        epoch += 1
        discovery?.cancel()
        discovery = nil
        // Losing the room is a reconnecting call, not a saved preference for a new call.
        publish(.pending, applied: nil)
        if value { schedule() }
    }

    func updatePeer(identity: String?, version: String?) {
        let value = identity.map { Peer(identity: $0, version: version) }
        guard !closed, value != peer else { return }
        peer = value
        epoch += 1
        discovery?.cancel()
        discovery = nil
        if connected { publish(.pending, applied: nil); schedule() }
    }

    func configure(_ value: AttentiveAudioInputConfiguration) async throws {
        try Task.checkCancellation()
        guard !closed else { throw CancellationError() }
        finishWaiters(.failure(AttentiveAudioInputError.superseded))
        revision += 1
        state = .init(status: connected || state.status != .idle ? .pending : .idle,
                      requested: value, applied: state.applied)
        // Accepting the request and sending it are independent of a caller waiting for it.
        // An observer can cancel that caller synchronously when pending is published.
        if connected { schedule() }
        onChange?(state)
        try Task.checkCancellation()
        guard !closed else { throw CancellationError() }
        guard connected else { return }
        try await waitForAcknowledgement(timeout: timeout)
    }

    /// Keep initial market noise out of the legacy worker detector until a non-default
    /// profile is acknowledged. Unsupported workers and rejected updates keep calls usable.
    /// The default profile has no additional microphone startup delay.
    func prepareToEnableMicrophone() async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        while state.requested != .standard, state.status == .pending {
            try Task.checkCancellation()
            guard !closed else { throw CancellationError() }
            let remaining = clock.now.duration(to: deadline)
            guard remaining > .zero else {
                publish(.failed, applied: state.applied, error: .timedOut)
                break
            }
            do { try await waitForAcknowledgement(timeout: remaining) }
            catch is CancellationError { throw CancellationError() }
            catch AttentiveAudioInputError.superseded { continue }
            catch { break }
        }
        try Task.checkCancellation()
        guard !closed else { throw CancellationError() }
    }

    private func waitForAcknowledgement(timeout: Duration) async throws {
        let id = UUID()
        try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                guard !Task.isCancelled else { continuation.resume(throwing: CancellationError()); return }
                waiters[id] = continuation
                deadlines[id] = Task { [weak self, timeout] in
                    do { try await Task.sleep(for: timeout) } catch { return }
                    guard let self, self.waiters[id] != nil else { return }
                    self.publish(.failed, applied: self.state.applied, error: .timedOut)
                    self.finishWaiters(.failure(AttentiveAudioInputError.timedOut))
                }
            }
        }, onCancel: { [weak self] in
            Task { @MainActor in self?.finishWaiter(id, result: .failure(CancellationError())) }
        })
    }

    func close() {
        closed = true
        connected = false
        epoch += 1
        discovery?.cancel()
        pump?.cancel()
        finishWaiters(.failure(CancellationError()))
        publish(.idle, applied: nil)
    }

    private func schedule() {
        guard connected, !closed else { return }
        guard let peer, peer.version == "1" else {
            guard discovery == nil else { return }
            discovery = Task { [weak self, timeout] in
                do { try await Task.sleep(for: timeout) } catch { return }
                guard let self, self.connected, !self.closed, self.peer?.version != "1" else { return }
                self.publish(.unsupported, applied: nil, error: .unsupported)
                self.finishWaiters(.failure(AttentiveAudioInputError.unsupported))
            }
            return
        }
        discovery?.cancel()
        discovery = nil
        guard pump == nil else { return }
        pump = Task { [weak self] in await self?.applyPending() }
    }

    private func applyPending() async {
        defer { pump = nil }
        while connected, !closed, let peer, peer.version == "1" {
            let sentRevision = revision
            let sentEpoch = epoch
            let requested = state.requested
            let result: Result<AttentiveAudioInputConfiguration, Error>
            do {
                let data = try JSONEncoder().encode(ListeningConfigurationMessage(requested))
                let payload = String(decoding: data, as: UTF8.self)
                let reply = try await send(peer.identity, payload)
                let applied = try ListeningConfigurationMessage.acknowledgement(reply)
                guard applied == requested else { throw AttentiveAudioInputError.invalidAcknowledgement }
                result = .success(applied)
            } catch { result = .failure(error) }
            guard connected, !closed else { return }
            // Reconnect, a changed agent, or a newer selection requires another serialized RPC.
            guard sentEpoch == epoch, sentRevision == revision else { continue }
            switch result {
            case .success(let applied):
                publish(.applied, applied: applied)
                finishWaiters(.success(()))
            case .failure(let error):
                let failure = error as? AttentiveAudioInputError ?? .updateFailed
                publish(failure == .unsupported ? .unsupported : .failed, applied: state.applied, error: failure)
                finishWaiters(.failure(failure))
            }
            return
        }
    }

    private func publish(_ status: AttentiveAudioInputState.Status, applied: AttentiveAudioInputConfiguration?,
                         error: AttentiveAudioInputError? = nil) {
        state = .init(status: status, requested: state.requested, applied: applied, message: error?.errorDescription)
        onChange?(state)
    }

    private func finishWaiters(_ result: Result<Void, Error>) {
        for id in Array(waiters.keys) { finishWaiter(id, result: result) }
    }

    private func finishWaiter(_ id: UUID, result: Result<Void, Error>) {
        deadlines.removeValue(forKey: id)?.cancel()
        waiters.removeValue(forKey: id)?.resume(with: result)
    }
}
