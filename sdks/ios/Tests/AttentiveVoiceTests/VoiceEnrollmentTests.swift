import Foundation
import XCTest
@testable import AttentiveVoice

final class VoiceEnrollmentTests: XCTestCase {
    func testStatusUsesNormalizedEmailAndExistingEndpoint() async throws {
        let provider = try makeProvider("status")
        let enrolled = try await provider.isEnrolled(email: "  Caller+Test@Example.com \n")
        XCTAssertTrue(enrolled)
    }

    func testUploadUsesWebMultipartContract() async throws {
        try await makeProvider("upload").enroll(email: " Caller+Test@Example.com ", wav: Self.wav)
    }

    func testRejectsInvalidEmailAndAudioBeforeSending() async throws {
        let provider = try makeProvider("must-not-send")
        for email in ["08123456789", "bad", "bad@example.com\r\nInjected: header"] {
            do { _ = try await provider.isEnrolled(email: email); XCTFail() }
            catch { XCTAssertEqual(error as? VoiceEnrollmentError, .invalidEmail) }
        }
        do { try await provider.enroll(email: "test@example.com", wav: Data()); XCTFail() }
        catch { XCTAssertEqual(error as? VoiceEnrollmentError, .invalidAudio) }
    }

    func testRejectsInsecureEndpointAndMalformedOrFailedResponses() async throws {
        XCTAssertThrowsError(try HTTPVoiceEnrollmentProvider(endpoint: URL(string: "http://localhost:3000")!))
        for (path, error) in [("failure", VoiceEnrollmentError.httpStatus(502)),
                              ("malformed", .invalidResponse), ("missing", .invalidResponse)] {
            do { _ = try await makeProvider(path).isEnrolled(email: "test@example.com"); XCTFail() }
            catch let received { XCTAssertEqual(received as? VoiceEnrollmentError, error) }
        }
        do { try await makeProvider("not-enrolled").enroll(email: "test@example.com", wav: Self.wav); XCTFail() }
        catch { XCTAssertEqual(error as? VoiceEnrollmentError, .invalidResponse) }
    }

    @MainActor
    func testRefreshDoesNotRecordOrAuthenticateAndRecordUsesSameIdentity() async {
        let provider = EnrollmentProviderStub()
        let recorder = EnrollmentRecorderStub()
        let flow = VoiceEnrollment(provider: provider, recorder: recorder)
        await flow.refresh(email: " CALLER@example.com ")
        XCTAssertTrue(flow.isEnrolled)
        XCTAssertEqual(recorder.recordings, 0)
        let initialUploads = await provider.uploads
        XCTAssertEqual(initialUploads, [])
        await flow.record(email: " CALLER@example.com ")
        XCTAssertEqual(flow.stage, .idle)
        XCTAssertTrue(flow.isEnrolled)
        XCTAssertEqual(flow.remainingSeconds, 0)
        XCTAssertFalse(flow.isBusy)
        let uploads = await provider.uploads
        XCTAssertEqual(uploads, ["caller@example.com"])
    }

    @MainActor
    func testFailedUploadDoesNotClaimEnrollmentAndCanRetry() async {
        let provider = EnrollmentProviderStub()
        await provider.setFailure(true)
        let flow = VoiceEnrollment(provider: provider, recorder: EnrollmentRecorderStub())
        await flow.record(email: "new@example.com")
        XCTAssertFalse(flow.isEnrolled)
        XCTAssertEqual(flow.stage, .failed)
        XCTAssertEqual(flow.lastError, .httpStatus(500))
        await provider.setFailure(false)
        await flow.record(email: "new@example.com")
        XCTAssertTrue(flow.isEnrolled)
        XCTAssertNil(flow.lastError)
    }

    @MainActor
    func testCancelledCaptureNeverUploadsAndUnlocksCallControls() async {
        let provider = EnrollmentProviderStub()
        let recorder = EnrollmentRecorderStub(delay: .seconds(20))
        let flow = VoiceEnrollment(provider: provider, recorder: recorder)
        let recording = Task { await flow.record(email: "caller@example.com") }
        while recorder.recordings == 0 { await Task.yield() }
        XCTAssertTrue(flow.isBusy)
        flow.cancel()
        await recording.value
        XCTAssertTrue(recorder.cancelled)
        XCTAssertFalse(flow.isBusy)
        XCTAssertFalse(flow.isEnrolled)
        XCTAssertNil(flow.lastError)
        let uploads = await provider.uploads
        XCTAssertEqual(uploads, [])
    }

    @MainActor
    func testMicrophoneDenialDoesNotUpload() async {
        let provider = EnrollmentProviderStub()
        let recorder = EnrollmentRecorderStub()
        recorder.denied = true
        let flow = VoiceEnrollment(provider: provider, recorder: recorder)
        await flow.record(email: "caller@example.com")
        XCTAssertEqual(flow.lastError, .microphoneDenied)
        XCTAssertFalse(flow.isEnrolled)
        let uploads = await provider.uploads
        XCTAssertTrue(uploads.isEmpty)
    }

    @MainActor
    func testOldStatusCannotEnrollDifferentIdentity() async {
        let flow = VoiceEnrollment(provider: EnrollmentProviderStub(), recorder: EnrollmentRecorderStub())
        let old = Task { await flow.refresh(email: "slow@example.com") }
        await Task.yield()
        await flow.refresh(email: "new@example.com")
        await old.value
        XCTAssertFalse(flow.isEnrolled)
        await flow.refresh(email: "caller@example.com")
        XCTAssertTrue(flow.isEnrolled)
        await flow.refresh(email: "08123456789")
        XCTAssertFalse(flow.isEnrolled)
    }

    static var wav: Data { Data("RIFF".utf8) + Data(repeating: 0, count: 4) + Data("WAVE".utf8) + Data(repeating: 0, count: 100) }

    private func makeProvider(_ path: String) throws -> HTTPVoiceEnrollmentProvider {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [EnrollmentURLProtocol.self]
        return try HTTPVoiceEnrollmentProvider(endpoint: URL(string: "https://example.com/\(path)")!,
                                                session: URLSession(configuration: config))
    }
}

private actor EnrollmentProviderStub: VoiceEnrollmentProvider {
    var uploads: [String] = []
    var fail = false
    func setFailure(_ value: Bool) { fail = value }
    func isEnrolled(email: String) async throws -> Bool {
        if email == "slow@example.com" { try await Task.sleep(for: .milliseconds(30)) }
        return email != "new@example.com"
    }
    func enroll(email: String, wav: Data) async throws {
        if fail { throw VoiceEnrollmentError.httpStatus(500) }
        uploads.append(email)
    }
}

@MainActor
private final class EnrollmentRecorderStub: VoiceEnrollmentRecording {
    var recordings = 0
    var cancelled = false
    var denied = false
    let delay: Duration
    init(delay: Duration = .zero) { self.delay = delay }
    func record(progress: @escaping @MainActor (Int) -> Void) async throws -> Data {
        recordings += 1
        if denied { throw VoiceEnrollmentError.microphoneDenied }
        progress(8)
        try await Task.sleep(for: delay)
        progress(0)
        return VoiceEnrollmentTests.wav
    }
    func cancel() { cancelled = true }
}

private final class EnrollmentURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.lastPathComponent
        var data = Data(#"{"enrolled":true}"#.utf8)
        var status = 200
        switch path {
        case "status":
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value,
                           "caller+test@example.com")
        case "upload":
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("multipart/form-data; boundary=") == true)
            var body = request.httpBody ?? Data()
            if let stream = request.httpBodyStream {
                stream.open()
                defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 4096)
                while stream.hasBytesAvailable {
                    let count = stream.read(&bytes, maxLength: bytes.count)
                    guard count > 0 else { break }
                    body.append(contentsOf: bytes.prefix(count))
                }
            }
            XCTAssertNotNil(body.range(of: Data("name=\"email\"\r\n\r\ncaller+test@example.com\r\n".utf8)))
            XCTAssertNotNil(body.range(of: Data("name=\"audio\"; filename=\"enroll.wav\"\r\nContent-Type: audio/wav".utf8)))
            XCTAssertNotNil(body.range(of: VoiceEnrollmentTests.wav))
        case "failure": status = 502; data = Data(#"{"detail":"private upstream information"}"#.utf8)
        case "malformed": data = Data("bad response".utf8)
        case "missing": data = Data("{}".utf8)
        case "not-enrolled": data = Data(#"{"enrolled":false}"#.utf8)
        default: XCTFail("Unexpected network request")
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
            httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
