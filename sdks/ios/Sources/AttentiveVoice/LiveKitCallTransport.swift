import AVFoundation
import Foundation
internal import LiveKit

@MainActor
final class LiveKitCallTransport: NSObject, CallTransport {
    var onEvent: ((CallEvent) -> Void)?
    private let room = Room()
    private var closing = false
    private var sawAgent = false
    private var audioProbe: FirstAudioProbe?

    override init() {
        super.init()
        room.add(delegate: self)
        audioProbe = FirstAudioProbe { [weak self] in
            Task { @MainActor in
                guard let self, !self.closing else { return }
                self.onEvent?(.agentAudioReceived)
            }
        }
    }

    func connect(_ credentials: CallCredentials, microphoneEnabled: Bool) async throws {
        try Task.checkCancellation()
        try await room.connect(url: credentials.serverUrl.absoluteString, token: credentials.participantToken)
        try Task.checkCancellation()
        guard !closing else { throw CancellationError() }
        for participant in room.remoteParticipants.values { updateAgent(participant) }
        if microphoneEnabled { try await setMicrophone(enabled: true) }
    }

    func disconnect() async {
        closing = true
        room.remove(delegate: self)
        await room.disconnect()
    }

    func setMicrophone(enabled: Bool) async throws {
        try await room.localParticipant.setMicrophone(enabled: enabled,
            captureOptions: AudioCaptureOptions(echoCancellation: true, autoGainControl: true, noiseSuppression: true))
    }

    func sendText(_ text: String) async throws -> String {
        try await room.localParticipant.sendText(text, for: "lk.chat").id
    }

    private func updateAgent(_ participant: Participant) {
        guard !closing, participant.kind == .agent else { return }
        sawAgent = true
        let value = AgentState(rawValue: participant.attributes["lk.agent.state"] ?? "") ?? .initializing
        onEvent?(.agentStateChanged(value))
    }
}

extension LiveKitCallTransport: RoomDelegate {
    nonisolated func room(_ room: Room, didUpdateConnectionState state: ConnectionState, from oldState: ConnectionState) {
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
            switch state {
            case .reconnecting: self.onEvent?(.stateChanged(.reconnecting))
            case .connected: self.onEvent?(.stateChanged(.connected))
            case .disconnected where oldState == .connected || oldState == .reconnecting:
                self.onEvent?(.stateChanged(.failed))
            default: break
            }
        }
    }

    nonisolated func room(_ room: Room, didStartReconnectWithMode mode: ReconnectMode) {
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
            self.onEvent?(.stateChanged(.reconnecting))
        }
    }

    nonisolated func room(_ room: Room, didCompleteReconnectWithMode mode: ReconnectMode) {
        Task { @MainActor [weak self] in
            guard let self, !self.closing else { return }
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
            audio.add(audioRenderer: probe)
        }
    }
}

/// Observes only the first non-silent buffer, without saving or modifying audio.
private final class FirstAudioProbe: NSObject, AudioRenderer, @unchecked Sendable {
    private let lock = NSLock()
    private var delivered = false
    private let callback: @Sendable () -> Void

    init(callback: @escaping @Sendable () -> Void) { self.callback = callback }

    func render(pcmBuffer: AVAudioPCMBuffer) {
        lock.lock()
        defer { lock.unlock() }
        guard !delivered else { return }
        let count = Int(pcmBuffer.frameLength)
        let audible: Bool
        if let samples = pcmBuffer.floatChannelData?[0] {
            audible = (0..<count).contains { abs(samples[$0]) > 0.0001 }
        } else if let samples = pcmBuffer.int16ChannelData?[0] {
            audible = (0..<count).contains { abs(Int(samples[$0])) > 3 }
        } else { audible = false }
        if audible { delivered = true; callback() }
    }
}
