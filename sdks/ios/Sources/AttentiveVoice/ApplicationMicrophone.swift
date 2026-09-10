import AVFoundation
import Combine
internal import AttentiveRTC

/// Native input mute can outlive a room even when its new microphone track is enabled.
@MainActor
final class ApplicationMicrophone {
    var onChange: ((Bool) -> Void)?
    private var trackEnabled = false
    private let isInputMuted: () -> Bool
    private let unmuteInput: () throws -> Void
    private var subscription: AnyCancellable?

    var enabled: Bool { trackEnabled && !isInputMuted() }

    init(isInputMuted: @escaping () -> Bool, unmuteInput: @escaping () throws -> Void) {
        self.isInputMuted = isInputMuted
        self.unmuteInput = unmuteInput
    }

    convenience init() {
        self.init(isInputMuted: {
            #if os(iOS)
            if #available(iOS 17, *), AVAudioApplication.shared.isInputMuted { return true }
            #endif
            return AudioManager.shared.isMicrophoneMuted
        }, unmuteInput: {
            #if os(iOS)
            if #available(iOS 17, *), AVAudioApplication.shared.isInputMuted {
                try AVAudioApplication.shared.setInputMuted(false)
            }
            #endif
            if AudioManager.shared.isMicrophoneMuted {
                AudioManager.shared.isMicrophoneMuted = false
                guard !AudioManager.shared.isMicrophoneMuted else { throw CallError.connectionFailed }
            }
        })
        #if os(iOS)
        if #available(iOS 17, *) {
            subscription = NotificationCenter.default.publisher(for: AVAudioApplication.inputMuteStateChangeNotification)
                .merge(with: NotificationCenter.default.publisher(for: AVAudioSession.routeChangeNotification),
                       NotificationCenter.default.publisher(for: AVAudioSession.interruptionNotification))
                .sink { [weak self] _ in
                    Task { @MainActor [weak self] in self?.refresh() }
                }
        }
        #endif
    }

    /// Only an explicit start/unmute may clear a previous application's input mute.
    func prepareToEnable() throws { try unmuteInput() }

    func updateTrack(enabled: Bool) {
        trackEnabled = enabled
        refresh()
    }

    func refresh() { onChange?(enabled) }

    func stop() {
        subscription = nil
        onChange = nil
        trackEnabled = false
    }
}
