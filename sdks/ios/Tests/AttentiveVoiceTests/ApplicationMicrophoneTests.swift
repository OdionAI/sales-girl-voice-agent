import AVFoundation
import XCTest
@testable import AttentiveVoice

final class ApplicationMicrophoneTests: XCTestCase {
    #if os(iOS)
    @MainActor
    func testNativeInputMuteIsClearedOnlyByExplicitEnable() throws {
        guard #available(iOS 17, *) else { return }
        let original = AVAudioApplication.shared.isInputMuted
        defer { try? AVAudioApplication.shared.setInputMuted(original) }
        try AVAudioApplication.shared.setInputMuted(true)
        let microphone = ApplicationMicrophone()
        microphone.updateTrack(enabled: true)
        XCTAssertFalse(microphone.enabled)
        XCTAssertTrue(AVAudioApplication.shared.isInputMuted)
        try microphone.prepareToEnable()
        XCTAssertTrue(microphone.enabled)
        XCTAssertFalse(AVAudioApplication.shared.isInputMuted)
        microphone.stop()
    }
    #endif

    @MainActor
    func testExplicitStartClearsMuteLeftByPreviousCall() throws {
        var muted = true
        let microphone = ApplicationMicrophone(isInputMuted: { muted }, unmuteInput: { muted = false })
        XCTAssertFalse(microphone.enabled)
        try microphone.prepareToEnable()
        microphone.updateTrack(enabled: true)
        XCTAssertTrue(microphone.enabled)
        XCTAssertFalse(muted)
    }

    @MainActor
    func testSystemMuteUpdatesReportedStateWithoutAutomaticallyUnmuting() throws {
        var muted = false
        let microphone = ApplicationMicrophone(isInputMuted: { muted }, unmuteInput: { muted = false })
        var events: [Bool] = []
        microphone.onChange = { events.append($0) }
        microphone.updateTrack(enabled: true)
        muted = true
        microphone.refresh()
        XCTAssertEqual(events, [true, false])
        XCTAssertTrue(muted)
        try microphone.prepareToEnable()
        microphone.updateTrack(enabled: true)
        XCTAssertTrue(microphone.enabled)
    }

    @MainActor
    func testExternalUnmuteDoesNotOverrideSDKMuteAndStopDropsEvents() {
        var muted = true
        let microphone = ApplicationMicrophone(isInputMuted: { muted }, unmuteInput: { muted = false })
        microphone.updateTrack(enabled: false)
        muted = false
        var events: [Bool] = []
        microphone.onChange = { events.append($0) }
        microphone.refresh()
        XCTAssertEqual(events, [false])
        microphone.stop()
        microphone.refresh()
        XCTAssertEqual(events, [false])
    }

    @MainActor
    func testUnmuteFailureIsNotReportedAsEnabled() {
        let microphone = ApplicationMicrophone(isInputMuted: { true }, unmuteInput: { throw CallError.connectionFailed })
        XCTAssertThrowsError(try microphone.prepareToEnable())
        XCTAssertFalse(microphone.enabled)
    }
}
