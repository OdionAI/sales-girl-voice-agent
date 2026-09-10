import AVFoundation
import Combine

/// iOS input muting is application-wide and can outlive an individual room or track.
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
            if #available(iOS 17, *) { return AVAudioApplication.shared.isInputMuted }
            #endif
            return false
        }, unmuteInput: {
            #if os(iOS)
            if #available(iOS 17, *), AVAudioApplication.shared.isInputMuted {
                try AVAudioApplication.shared.setInputMuted(false)
            }
            #endif
        })
        #if os(iOS)
        if #available(iOS 17, *) {
            subscription = NotificationCenter.default.publisher(for: AVAudioApplication.inputMuteStateChangeNotification)
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
