import AttentiveVoice
import AttentiveVoiceUI
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
        let env = SampleLocalLaunchSettings.environment()
        return Self(
            endpoint: env["ATTENTIVE_CALL_ENDPOINT"] ?? "https://attentive.odion.ai/api/public-agent/connection-details",
            business: env["ATTENTIVE_BUSINESS_SLUG"] ?? "wema-bank-poc-local",
            agent: env["ATTENTIVE_AGENT_ID"] ?? "agt_73099afb71",
            contact: env["ATTENTIVE_CALLER_CONTACT"] ?? "sdk-demo@example.com",
            customerID: env["ATTENTIVE_CUSTOMER_ID"] ?? "",
            phone: env["ATTENTIVE_PHONE"] ?? "",
            account: env["ATTENTIVE_ACCOUNT"] ?? "",
            microphoneOnStart: !ProcessInfo.processInfo.arguments.contains("--chat-only")
        )
    }

    var request: CallRequest {
        get {
            let profile: CallerProfile? = customerID.isEmpty && phone.isEmpty && account.isEmpty ? nil : .init(
                customerId: customerID, accountNumber: account.isEmpty ? nil : account,
                phoneNumber: phone.isEmpty ? nil : phone)
            return .init(businessSlug: business, agentPublicId: agent, endUserContact: contact,
                         profile: profile, toolWaitSpeechMode: waitMode)
        }
        set {
            business = newValue.businessSlug
            agent = newValue.agentPublicId
            contact = newValue.endUserContact
            customerID = newValue.wemaContext?.customerId ?? ""
            account = newValue.wemaContext?.accountNumber ?? ""
            phone = newValue.wemaContext?.phoneNumber ?? ""
            waitMode = newValue.toolWaitSpeechMode ?? .toolSpecific
        }
    }

    @MainActor
    func makeCall() throws -> AttentiveCall {
        guard let url = URL(string: endpoint) else { throw CallError.invalidRequest }
        let localDevice = LocalDeviceConnectionPolicy(endpoint: url)
        let provider = try HTTPCallCredentialProvider(endpoint: url,
            allowsInsecureDevelopmentConnections: allowsInsecureDevelopment || localDevice.enabled)
        return AttentiveCall(credentialProvider: SampleCredentialProvider(provider: provider, localDevice: localDevice),
                            allowsInsecureDevelopmentConnections: allowsInsecureDevelopment || localDevice.enabled)
    }

    @MainActor
    func makeEnrollment() throws -> VoiceEnrollment {
        guard let url = URL(string: endpoint) else { throw CallError.invalidRequest }
        let localDevice = LocalDeviceConnectionPolicy(endpoint: url)
        let provider = try HTTPVoiceEnrollmentProvider(
            endpoint: url.deletingLastPathComponent().appendingPathComponent("voice-enroll"),
            allowsInsecureDevelopmentConnections: allowsInsecureDevelopment || localDevice.enabled)
        return VoiceEnrollment(provider: provider, recorder: IOSVoiceEnrollmentRecorder())
    }

    private var allowsInsecureDevelopment: Bool {
        #if DEBUG && targetEnvironment(simulator)
        return URL(string: endpoint)?.scheme == "http"
        #else
        return false
        #endif
    }
}

private struct SampleCredentialProvider: CallCredentialProvider {
    let provider: HTTPCallCredentialProvider
    let localDevice: LocalDeviceConnectionPolicy

    func fetch(for request: CallRequest) async throws -> CallCredentials {
        let credentials = try await provider.fetch(for: request)
        return CallCredentials(serverUrl: localDevice.signalingURL(credentials.serverUrl),
                               roomName: credentials.roomName, participantToken: credentials.participantToken)
    }
}

@MainActor
final class SampleModel: ObservableObject {
    @Published var configuration: SampleConfiguration
    @Published var call: AttentiveCall
    @Published var enrollment: VoiceEnrollment
    @Published var errorMessage: String?
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

    var callerEnrollment: VoiceEnrollment? {
        // The public Lagos deployment does not expose enrollment yet; local testing still does.
        URL(string: configuration.endpoint)?.host?.lowercased() == "attentive.odion.ai" ? nil : enrollment
    }

    var appearance: CallerUIConfiguration {
        if ProcessInfo.processInfo.arguments.contains("--generic-ui") { return .init() }
        return .init(title: "Talk to Wema Bank", agentName: "SAW", profileTitle: "My Wema",
                     customerIDLabel: "Wema customer ID", activityTitle: "Bank activity",
                     emptyActivityText: "No bank activity yet", showsProfile: true,
                     toolDisplayNames: [
                        "wema_get_balance": "Check balance", "wema_get_transactions": "Transaction history",
                        "wema_prepare_transfer": "Prepare transfer", "wema_prepare_airtime": "Prepare airtime",
                        "wema_prepare_data_purchase": "Prepare data purchase", "wema_list_data_plans": "Find data plans",
                        "wema_execute_prepared": "Execute prepared request", "wema_list_transfer_banks": "Find transfer bank",
                     ])
    }

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
        } catch { errorMessage = error.localizedDescription }
    }

    private func observeCall() {
        call.onEvent = { [weak self] event in
            guard let self else { return }
            switch event {
            case .stateChanged(let state): logger.info("call_state=\(state.rawValue, privacy: .public)")
            case .agentStateChanged(let state): logger.info("agent_state=\(state.rawValue, privacy: .public)")
            case .agentAudioReceived:
                logger.info("non_silent_agent_audio_received")
            case .transcript(let item) where item.isFinal:
                logger.info("final_transcript speaker=\(item.speaker.rawValue, privacy: .public) characters=\(item.text.count)")
            case .toolActivity(let activity):
                logger.info("tool_event=\(activity.event, privacy: .public) status=\(activity.status, privacy: .public)")
            case .failure: logger.error("call_failed")
            default: break
            }
        }
    }
}
