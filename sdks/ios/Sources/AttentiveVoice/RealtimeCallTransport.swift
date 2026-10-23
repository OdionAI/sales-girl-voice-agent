import AVFoundation
import Foundation
internal import AttentiveRTC

@MainActor
final class RealtimeCallTransport: NSObject, CallTransport {
    var onEvent: ((CallEvent) -> Void)?
    var onPasskeyEvent: ((PasskeyTransportEvent) -> Void)?
    private var passkeysEnabled = false
    private var passkeyDiagnostics: PasskeyDiagnostics?
    private var passkeyPeer: Participant.Identity?
    private var transportConnected = false
    private static let passkeyTopic = "attentive.fido2.v1"
    private let room = Room()
    private var closing = false
    private var sawAgent = false
    private var audioProbe: AgentAudioProbe?
    private var observedAudioTracks: [RemoteAudioTrack] = []
    private let microphone = ApplicationMicrophone()
    var microphoneEnabled: Bool? { microphone.enabled }
    private lazy var listening = AudioInputNegotiator { [weak self] identity, payload in
        guard let self, !self.closing else { throw CancellationError() }
        do {
            return try await self.room.localParticipant.performRpc(
                destinationIdentity: Participant.Identity(from: identity),
                method: "attentive.listening.configure", payload: payload,
                responseTimeout: 8, maxRoundTripLatency: 3)
        } catch let error as RpcError {
            switch error.code {
            case 1400, 1403, 1404: throw AttentiveAudioInputError.unsupported
            case 1501, 1502: throw AttentiveAudioInputError.timedOut
            case 409: throw AttentiveAudioInputError.callerSpeaking
            case 503: throw AttentiveAudioInputError.processingUnavailable
            default: throw AttentiveAudioInputError.updateFailed
            }
        }
    }
    private lazy var microphoneActivation = MicrophoneActivation(prepareEnable: { [weak self] in
        guard let self, !self.closing else { throw CancellationError() }
        try await self.listening.prepareToEnableMicrophone()
    }, publish: { [weak self] enabled in
        guard let self, !self.closing else { throw CancellationError() }
        if enabled { try self.microphone.prepareToEnable() }
        try await self.room.localParticipant.setMicrophone(enabled: enabled,
            captureOptions: AudioCaptureOptions(echoCancellation: true, autoGainControl: true, noiseSuppression: true))
    }, didApply: { [weak self] enabled in
        guard let self, !self.closing else { throw CancellationError() }
        // Publishing can reuse an audio engine whose input was muted by the previous room.
        if enabled { try self.microphone.prepareToEnable() }
        self.microphone.updateTrack(enabled: enabled)
    })

    override init() {
        super.init()
        room.add(delegate: self)
        listening.onChange = { [weak self] state in
            guard let self, !self.closing else { return }
            self.onEvent?(.audioInputChanged(state))
        }
        microphone.onChange = { [weak self] enabled in
            guard let self, !self.closing else { return }
            self.onEvent?(.microphoneChanged(enabled))
        }
        audioProbe = AgentAudioProbe(firstAudio: { [weak self] in
            Task { @MainActor in
                guard let self, !self.closing else { return }
                self.onEvent?(.agentAudioReceived)
            }
        }, energy: { [weak self] energy in
            Task { @MainActor in
                guard let self, !self.closing else { return }
                self.onEvent?(.agentAudioEnergy(energy))
            }
        })
    }

    func connect(_ credentials: CallCredentials, microphoneEnabled: Bool) async throws {
        try Task.checkCancellation()
        try await room.connect(url: credentials.serverUrl.absoluteString, token: credentials.participantToken)
        try Task.checkCancellation()
        guard !closing else { throw CancellationError() }
        transportConnected = true
        listening.setConnected(true)
        for participant in room.remoteParticipants.values { updateAgent(participant) }
        if microphoneEnabled { try await setMicrophone(enabled: true) }
    }

    func disconnect() async {
        closing = true
        transportConnected = false
        passkeyPeer = nil
        onPasskeyEvent?(.unavailable)
        microphoneActivation.close()
        listening.close()
        microphone.stop()
        if let probe = audioProbe {
            for track in observedAudioTracks { track.remove(audioRenderer: probe) }
        }
        observedAudioTracks.removeAll()
        room.remove(delegate: self)
        await room.disconnect()
    }

    func setMicrophone(enabled: Bool) async throws {
        try await microphoneActivation.setEnabled(enabled)
    }

    func sendText(_ text: String) async throws -> String {
        try await room.localParticipant.sendText(text, for: "lk.chat").id
    }

    func setPasskeysEnabled(_ enabled: Bool) { passkeysEnabled = enabled }
    func setPasskeyDiagnostics(_ diagnostics: PasskeyDiagnostics) { passkeyDiagnostics = diagnostics }

    func sendPasskeyMessage(_ data: Data) async throws {
        guard !closing, transportConnected, passkeysEnabled, let passkeyPeer, data.count <= 14_000 else {
            passkeyDiagnostics?.record(.connection, .failed, reason: .unavailable)
            throw PasskeyFailure.unavailable
        }
        try await room.localParticipant.publish(data: data, options: DataPublishOptions(
            destinationIdentities: [passkeyPeer], topic: Self.passkeyTopic, reliable: true))
    }

    private func updatePasskeyPeer() {
        guard passkeysEnabled, transportConnected, !closing else { return }
        let agents = room.remoteParticipants.values.filter { $0.kind == .agent }
        let peer = agents.count == 1 ? agents.first?.identity : nil
        if peer == nil { passkeyDiagnostics?.record(.connection, .ignored, reason: .unavailable) }
        guard peer != passkeyPeer else { return }
        if passkeyPeer != nil { onPasskeyEvent?(.unavailable) }
        passkeyPeer = peer
        if peer != nil { onPasskeyEvent?(.ready) }
    }

    func setAudioInputConfiguration(_ configuration: AttentiveAudioInputConfiguration) async throws {
        try await listening.configure(configuration)
    }

    private func updateAgent(_ participant: Participant) {
        guard !closing, participant.kind == .agent else { return }
        updatePasskeyPeer()
        sawAgent = true
        listening.updatePeer(identity: participant.identity?.stringValue,
                             version: participant.attributes["attentive.listening.version"],
                             numericVersion: participant.attributes["attentive.listening.numericVersion"])
        let value = AgentState(rawValue: participant.attributes["lk.agent.state"] ?? "") ?? .initializing
        onEvent?(.agentStateChanged(value))
    }
}

extension RealtimeCallTransport: RoomDelegate {
    nonisolated func room(_ room: Room, didUpdateConnectionState state: ConnectionState, from oldState: ConnectionState) {
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
            switch state {
            case .reconnecting:
                self.transportConnected = false
                self.passkeyPeer = nil
                self.onPasskeyEvent?(.unavailable)
                self.listening.setConnected(false)
                self.onEvent?(.stateChanged(.reconnecting))
            case .connected:
                self.transportConnected = true
                self.updatePasskeyPeer()
                self.listening.setConnected(true)
                self.onEvent?(.stateChanged(.connected))
            case .disconnected where oldState == .connected || oldState == .reconnecting:
                self.transportConnected = false
                self.passkeyPeer = nil
                self.onPasskeyEvent?(.unavailable)
                self.onEvent?(.stateChanged(.failed))
            default: break
            }
        }
    }

    nonisolated func room(_ room: Room, didStartReconnectWithMode mode: ReconnectMode) {
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
            self.listening.setConnected(false)
            self.transportConnected = false
            self.passkeyPeer = nil
            self.onPasskeyEvent?(.unavailable)
            self.onEvent?(.stateChanged(.reconnecting))
        }
    }

    nonisolated func room(_ room: Room, didCompleteReconnectWithMode mode: ReconnectMode) {
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
            self.microphone.refresh()
            self.transportConnected = true
            for participant in self.room.remoteParticipants.values { self.updateAgent(participant) }
            self.listening.setConnected(true)
            self.onEvent?(.stateChanged(.connected))
        }
    }

    nonisolated func room(_ room: Room, participantDidConnect participant: RemoteParticipant) {
        Task { @MainActor [weak self] in self?.updateAgent(participant) }
    }

    nonisolated func room(_ room: Room, participant: Participant, didUpdateAttributes attributes: [String: String]) {
        Task { @MainActor [weak self] in self?.updateAgent(participant) }
    }

    nonisolated func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        guard participant.kind == .agent else { return }
        Task { @MainActor [weak self] in
            guard let self, !self.closing, self.sawAgent else { return }
            self.passkeyPeer = nil
            self.onPasskeyEvent?(.unavailable)
            self.listening.updatePeer(identity: nil, version: nil)
            self.onEvent?(.agentStateChanged(.disconnected))
        }
    }

    nonisolated func room(_ room: Room, participant: Participant, trackPublication: TrackPublication,
                          didReceiveTranscriptionSegments segments: [TranscriptionSegment]) {
        let speaker: Transcript.Speaker = participant.kind == .agent ? .agent : .caller
        let identity = participant.identity?.stringValue ?? speaker.rawValue
        let transcripts = segments.filter { !$0.text.isEmpty }.map {
            Transcript(id: "\(identity):\($0.id)", speaker: speaker, text: $0.text, isFinal: $0.isFinal)
        }
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
            for transcript in transcripts { self.onEvent?(.transcript(transcript)) }
        }
    }

    nonisolated func room(_ room: Room, participant: RemoteParticipant?, didReceiveData data: Data,
                          forTopic topic: String, encryptionType: EncryptionType) {
        if topic == "attentive.fido2.v1" {
            guard participant?.kind == .agent, let identity = participant?.identity, data.count <= 14_000 else {
                Task { @MainActor [weak self] in self?.passkeyDiagnostics?.record(.receipt, .ignored, reason: .invalidRequest) }
                return
            }
            Task { @MainActor [weak self] in
                guard let self, !self.closing, self.transportConnected, self.passkeysEnabled,
                      identity == self.passkeyPeer else {
                    self?.passkeyDiagnostics?.record(.receipt, .ignored, reason: .unavailable); return
                }
                self.onPasskeyEvent?(.message(data))
            }
            return
        }
        // Only the server-side agent can supply tool/auth observations.
        guard participant?.kind == .agent, let event = BackendEventDecoder.decode(data: data, topic: topic) else { return }
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
            self.onEvent?(event)
        }
    }

    nonisolated func room(_ room: Room, participant: RemoteParticipant, didSubscribeTrack publication: RemoteTrackPublication) {
        guard participant.kind == .agent, let audio = publication.track as? RemoteAudioTrack else { return }
        Task { @MainActor [weak self] in
            guard let self, !self.closing, let probe = self.audioProbe else { return }
            guard !self.observedAudioTracks.contains(where: { $0 === audio }) else { return }
            self.observedAudioTracks.append(audio)
            audio.add(audioRenderer: probe)
        }
    }
}

/// Observes agent PCM before playback, without saving or modifying audio.
private final class AgentAudioProbe: NSObject, AudioRenderer, @unchecked Sendable {
    private let lock = NSLock()
    private var delivered = false
    private var level = AudioBufferLevel()
    private var lastEmission: TimeInterval = 0
    private let firstAudio: @Sendable () -> Void
    private let energy: @Sendable (Float) -> Void

    init(firstAudio: @escaping @Sendable () -> Void, energy: @escaping @Sendable (Float) -> Void) {
        self.firstAudio = firstAudio
        self.energy = energy
    }

    func render(pcmBuffer: AVAudioPCMBuffer) {
        lock.lock()
        level.include(pcmBuffer)
        let first = !delivered && level.peak > 0.0001
        if first { delivered = true }
        let now = ProcessInfo.processInfo.systemUptime
        let emit = now - lastEmission >= 0.04
        let value = level.energy
        if emit { level = AudioBufferLevel(); lastEmission = now }
        lock.unlock()
        if first { firstAudio() }
        if emit { energy(value) }
    }
}
