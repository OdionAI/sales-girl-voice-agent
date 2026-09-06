import Combine
import Foundation

public enum VoiceEnrollmentError: Error, LocalizedError, Equatable {
    case invalidEmail, invalidAudio, invalidResponse, recordingFailed, microphoneDenied, serviceUnavailable
    case httpStatus(Int)

    public var errorDescription: String? {
        switch self {
        case .invalidEmail: return "Enter a valid email for voice enrollment."
        case .invalidAudio: return "The voice recording is incomplete. Please record again."
        case .invalidResponse: return "The enrollment service returned an invalid response."
        case .recordingFailed: return "The voice recording was interrupted. Please try again."
        case .microphoneDenied: return "Allow microphone access in Settings to record your voice."
        case .serviceUnavailable: return "Voice enrollment is unavailable. Please try again."
        case .httpStatus(let code): return "Voice enrollment failed (HTTP \(code)). Please try again."
        }
    }
}

public protocol VoiceEnrollmentProvider: Sendable {
    func isEnrolled(email: String) async throws -> Bool
    func enroll(email: String, wav: Data) async throws
}

/// Uses the same enrollment endpoint as the web caller. Enrollment is not live authentication.
public struct HTTPVoiceEnrollmentProvider: VoiceEnrollmentProvider {
    private let endpoint: URL
    private let session: URLSession

    public init(endpoint: URL, allowsInsecureDevelopmentConnections: Bool = false,
                session: URLSession? = nil) throws {
        try validateURL(endpoint, schemes: ["https", "http"], allowInsecure: allowsInsecureDevelopmentConnections)
        self.endpoint = endpoint
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        self.session = session ?? URLSession(configuration: config, delegate: NoCallRedirects(), delegateQueue: nil)
    }

    public func isEnrolled(email: String) async throws -> Bool {
        let email = try normalizedEnrollmentEmail(email)
        var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = (components.queryItems ?? []).filter { $0.name != "email" }
            + [URLQueryItem(name: "email", value: email)]
        let request = URLRequest(url: components.url!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        return try await response(request).enrolled
    }

    public func enroll(email: String, wav: Data) async throws {
        let email = try normalizedEnrollmentEmail(email)
        guard wav.count > 44, wav.count <= 3_000_000,
              wav.prefix(4) == Data("RIFF".utf8), wav[8..<12] == Data("WAVE".utf8) else {
            throw VoiceEnrollmentError.invalidAudio
        }
        let boundary = "Attentive-\(UUID().uuidString)"
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"email\"\r\n\r\n\(email)\r\n".utf8)
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"enroll.wav\"\r\nContent-Type: audio/wav\r\n\r\n".utf8))
        body.append(wav)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var request = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 60)
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        guard try await response(request).enrolled else { throw VoiceEnrollmentError.invalidResponse }
    }

    private struct Response: Decodable { let enrolled: Bool }

    private func response(_ request: URLRequest) async throws -> Response {
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw VoiceEnrollmentError.invalidResponse }
        guard response.statusCode == 200 else { throw VoiceEnrollmentError.httpStatus(response.statusCode) }
        guard data.count <= 65_536, let result = try? JSONDecoder().decode(Response.self, from: data) else {
            throw VoiceEnrollmentError.invalidResponse
        }
        return result
    }
}

func normalizedEnrollmentEmail(_ value: String) throws -> String {
    let email = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    guard email.range(of: #"^[^@\s]+@[^@\s]+\.[^@\s]+$"#, options: .regularExpression) != nil else {
        throw VoiceEnrollmentError.invalidEmail
    }
    return email
}

@MainActor
public protocol VoiceEnrollmentRecording: AnyObject {
    func record(progress: @escaping @MainActor (Int) -> Void) async throws -> Data
    func cancel()
}

/// A pre-call flow, independent of the live Session/Action authentication checks.
/// Do not record during a call. Cancel when leaving the screen or entering background.
@MainActor
public final class VoiceEnrollment: ObservableObject {
    public enum Stage { case idle, checking, recording, saving, failed }
    @Published public private(set) var stage: Stage = .idle
    @Published public private(set) var isEnrolled = false
    @Published public private(set) var remainingSeconds = 8
    @Published public private(set) var lastError: VoiceEnrollmentError?
    public var isBusy: Bool { stage == .recording || stage == .saving }

    private let provider: any VoiceEnrollmentProvider
    private let recorder: any VoiceEnrollmentRecording
    private var email = ""
    private var revision = UUID()
    private var operation: Task<Void, Never>?

    public init(provider: any VoiceEnrollmentProvider, recorder: any VoiceEnrollmentRecording) {
        self.provider = provider
        self.recorder = recorder
    }

    public func refresh(email value: String) async {
        guard !isBusy else { return }
        let normalized = try? normalizedEnrollmentEmail(value)
        if email != normalized { isEnrolled = false }
        email = normalized ?? ""
        lastError = nil
        revision = UUID()
        let id = revision
        guard let normalized else { stage = .idle; return }
        stage = .checking
        do {
            let result = try await provider.isEnrolled(email: normalized)
            guard revision == id, !Task.isCancelled else { return }
            isEnrolled = result
            stage = .idle
        } catch {
            guard revision == id, !Task.isCancelled else { return }
            lastError = error as? VoiceEnrollmentError ?? .serviceUnavailable
            stage = .failed
        }
    }

    public func record(email value: String) async {
        guard !isBusy else { return }
        guard let normalized = try? normalizedEnrollmentEmail(value) else {
            lastError = .invalidEmail; stage = .failed; return
        }
        if email != normalized { isEnrolled = false }
        email = normalized
        revision = UUID()
        let id = revision
        remainingSeconds = 8
        lastError = nil
        stage = .recording
        let task = Task { [self] in
            do {
                let wav = try await recorder.record { [weak self] seconds in
                    guard self?.revision == id else { return }
                    self?.remainingSeconds = seconds
                }
                try Task.checkCancellation()
                guard revision == id else { return }
                stage = .saving
                try await provider.enroll(email: normalized, wav: wav)
                try Task.checkCancellation()
                guard revision == id else { return }
                isEnrolled = true
                stage = .idle
            } catch {
                guard revision == id else { return }
                if error is CancellationError { stage = .idle }
                else { lastError = error as? VoiceEnrollmentError ?? .serviceUnavailable; stage = .failed }
            }
        }
        operation = task
        await withTaskCancellationHandler(operation: { await task.value }, onCancel: { task.cancel() })
        if revision == id { operation = nil }
    }

    public func cancel() {
        revision = UUID()
        operation?.cancel()
        operation = nil
        recorder.cancel()
        stage = .idle
        lastError = nil
    }
}
