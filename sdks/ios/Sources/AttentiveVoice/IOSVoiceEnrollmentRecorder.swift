#if os(iOS)
import AVFoundation
import Foundation

/// Explicit, eight-second pre-call recording. Never runs as a LiveKit audio tap.
@MainActor
public final class IOSVoiceEnrollmentRecorder: VoiceEnrollmentRecording {
    private var recorder: AVAudioRecorder?
    private var directory: URL?
    private var restoreSession: (() -> Void)?
    private var attempt: UUID?

    public init() {}

    public func record(progress: @escaping @MainActor (Int) -> Void) async throws -> Data {
        guard attempt == nil else { throw VoiceEnrollmentError.recordingFailed }
        let id = UUID()
        attempt = id
        defer { if attempt == id { cancel() } }
        let permitted: Bool
        if #available(iOS 17, *) { permitted = await AVAudioApplication.requestRecordPermission() }
        else { permitted = await AVCaptureDevice.requestAccess(for: .audio) }
        try Task.checkCancellation()
        guard attempt == id else { throw CancellationError() }
        guard permitted else { throw VoiceEnrollmentError.microphoneDenied }

        let session = AVAudioSession.sharedInstance()
        let category = session.category
        let mode = session.mode
        let options = session.categoryOptions
        // Release and restore the pre-call audio session before handing control to LiveKit.
        restoreSession = {
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            try? session.setCategory(category, mode: mode, options: options)
        }
        try session.setCategory(.record, mode: .default)
        try session.setActive(true)
        let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("attentive-enrollment-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700, .protectionKey: FileProtectionType.complete])
        directory = temporary
        let url = temporary.appendingPathComponent("voice.wav")
        let recording = try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsFloatKey: false,
        ])
        recorder = recording
        guard recording.prepareToRecord(), recording.record(forDuration: 8) else {
            throw VoiceEnrollmentError.recordingFailed
        }
        for remaining in stride(from: 8, through: 1, by: -1) {
            try Task.checkCancellation()
            guard attempt == id, recording.isRecording else { throw VoiceEnrollmentError.recordingFailed }
            progress(remaining)
            try await Task.sleep(for: .seconds(1))
        }
        try Task.checkCancellation()
        guard attempt == id else { throw CancellationError() }
        recording.stop()
        progress(0)
        let file = try AVAudioFile(forReading: url)
        guard Double(file.length) / file.processingFormat.sampleRate >= 7.5 else { throw VoiceEnrollmentError.invalidAudio }
        return try Data(contentsOf: url)
    }

    public func cancel() {
        attempt = nil
        recorder?.stop()
        recorder = nil
        if let directory { try? FileManager.default.removeItem(at: directory) }
        directory = nil
        restoreSession?()
        restoreSession = nil
    }
}
#endif
