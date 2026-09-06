import AttentiveVoice
import Combine
import Foundation
import OSLog

struct SampleConfiguration {
    var endpoint: String
    var business: String
    var agent: String
    var contact: String
    var customerID: String
    var phone: String
    var account: String
    var waitMode: ToolWaitSpeechMode = .toolSpecific
    var microphoneOnStart = true

    static var initial: Self {
        let env = ProcessInfo.processInfo.environment
        return Self(
            endpoint: env["ATTENTIVE_CALL_ENDPOINT"] ?? "http://127.0.0.1:3000/api/public-agent/connection-details",
            business: env["ATTENTIVE_BUSINESS_SLUG"] ?? "wema-bank-poc-local",
            agent: env["ATTENTIVE_AGENT_ID"] ?? "agt_59a007e81e",
            contact: env["ATTENTIVE_CALLER_CONTACT"] ?? "sdk-demo@example.com",
            customerID: env["ATTENTIVE_CUSTOMER_ID"] ?? "",
            phone: env["ATTENTIVE_PHONE"] ?? "",
            account: env["ATTENTIVE_ACCOUNT"] ?? "",
            microphoneOnStart: !ProcessInfo.processInfo.arguments.contains("--chat-only")
        )
    }

    var request: CallRequest {
        let profile: CallerProfile? = customerID.isEmpty ? nil : .init(
            customerId: customerID, accountNumber: account.isEmpty ? nil : account,
            phoneNumber: phone.isEmpty ? nil : phone)
        return .init(businessSlug: business, agentPublicId: agent, endUserContact: contact,
                     profile: profile, toolWaitSpeechMode: waitMode)
    }

    @MainActor
    func makeCall() throws -> AttentiveCall {
        guard let url = URL(string: endpoint) else { throw CallError.invalidRequest }
        #if DEBUG && targetEnvironment(simulator)
        let insecureDevelopment = true
        #else
        let insecureDevelopment = false
        #endif
        return try AttentiveCall(endpoint: url, allowsInsecureDevelopmentConnections: insecureDevelopment)
    }

    @MainActor
    func makeEnrollment() throws -> VoiceEnrollment {
        guard let url = URL(string: endpoint) else { throw CallError.invalidRequest }
        #if DEBUG && targetEnvironment(simulator)
        let insecureDevelopment = true
        #else
        let insecureDevelopment = false
        #endif
        let provider = try HTTPVoiceEnrollmentProvider(
            endpoint: url.deletingLastPathComponent().appendingPathComponent("voice-enroll"),
            allowsInsecureDevelopmentConnections: insecureDevelopment)
        return VoiceEnrollment(provider: provider, recorder: IOSVoiceEnrollmentRecorder())
    }
}

@MainActor
final class SampleModel: ObservableObject {
    @Published var configuration: SampleConfiguration
    @Published var call: AttentiveCall
    @Published var enrollment: VoiceEnrollment
    @Published var errorMessage: String?
    @Published var receivedAudio = false
    @Published var startedAt: Date?
    @Published var sending = false
    private let logger = Logger(subsystem: "ai.odion.attentive.sample", category: "call")

    init() {
        let config = SampleConfiguration.initial
        configuration = config
        do {
            let newCall = try config.makeCall()
            enrollment = try config.makeEnrollment()
            call = newCall
        }
        catch {
            // Keep the screen usable so a malformed launch configuration can be corrected.
            call = try! AttentiveCall(endpoint: URL(string: "https://example.invalid/api/call")!)
            enrollment = VoiceEnrollment(provider: try! HTTPVoiceEnrollmentProvider(
                endpoint: URL(string: "https://example.invalid/api/voice-enroll")!), recorder: IOSVoiceEnrollmentRecorder())
            errorMessage = error.localizedDescription
        }
        observeCall()
    }

    var active: Bool { [.connecting, .connected, .reconnecting, .ending].contains(call.state) }

    func apply(_ config: SampleConfiguration) {
        guard !active, !enrollment.isBusy else { return }
        do {
            let replacement = try config.makeCall()
            let replacementEnrollment = try config.makeEnrollment()
            enrollment.cancel()
            call.onEvent = nil
            configuration = config
            call = replacement
            enrollment = replacementEnrollment
            observeCall()
            errorMessage = nil
            receivedAudio = false
        } catch { errorMessage = error.localizedDescription }
    }

    func start() async {
        guard !enrollment.isBusy else { return }
        // Enrollment is keyed by the same normalized email the caller sends to the backend.
        if configuration.contact.contains("@") {
            configuration.contact = configuration.contact.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        }
        receivedAudio = false
        errorMessage = nil
        startedAt = Date()
        do { try await call.start(configuration.request, microphoneEnabled: configuration.microphoneOnStart) }
        catch is CancellationError {}
        catch { errorMessage = error.localizedDescription }
    }

    func end() async { await call.end() }

    func toggleMicrophone() async {
        do { try await call.setMicrophone(enabled: !call.microphoneEnabled) }
        catch { errorMessage = error.localizedDescription }
    }

    func send(_ text: String) async -> Bool {
        sending = true
        defer { sending = false }
        do { try await call.sendText(text); return true }
        catch { errorMessage = error.localizedDescription; return false }
    }

    private func observeCall() {
        call.onEvent = { [weak self] event in
            guard let self else { return }
            switch event {
            case .stateChanged(let state): logger.info("call_state=\(state.rawValue, privacy: .public)")
            case .agentStateChanged(let state): logger.info("agent_state=\(state.rawValue, privacy: .public)")
            case .agentAudioReceived:
                receivedAudio = true
                logger.info("non_silent_agent_audio_received")
            case .transcript(let item) where item.isFinal:
                logger.info("final_transcript speaker=\(item.speaker.rawValue, privacy: .public) characters=\(item.text.count)")
            case .toolActivity(let activity):
                logger.info("tool_event=\(activity.event, privacy: .public) status=\(activity.status, privacy: .public)")
            case .failure(let error): errorMessage = error.localizedDescription
            default: break
            }
        }
    }
}
