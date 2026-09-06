import AttentiveVoice
import Darwin
import Foundation

@main
struct AttentiveSmoke {
    @MainActor
    static func main() async {
        do { try await run() }
        catch {
            log("FAIL: \(error.localizedDescription)")
            exit(1)
        }
    }

    private static func log(_ text: String) {
        FileHandle.standardOutput.write(Data((text + "\n").utf8))
    }

    @MainActor
    private static func run() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let endpoint = env["ATTENTIVE_CALL_ENDPOINT"].flatMap(URL.init(string:)),
              let business = env["ATTENTIVE_BUSINESS_SLUG"],
              let agent = env["ATTENTIVE_AGENT_ID"],
              let contact = env["ATTENTIVE_CALLER_CONTACT"] else {
            log("Set ATTENTIVE_CALL_ENDPOINT, ATTENTIVE_BUSINESS_SLUG, ATTENTIVE_AGENT_ID, ATTENTIVE_CALLER_CONTACT.")
            throw CallError.invalidRequest
        }
        let call = try AttentiveCall(endpoint: endpoint,
            allowsInsecureDevelopmentConnections: env["ATTENTIVE_ALLOW_INSECURE"] == "1")
        var heardAudio = false
        var transcriptCount = 0
        call.onEvent = { event in
            switch event {
            case .stateChanged(let state): log("call: \(state.rawValue)")
            case .agentStateChanged(let state): log("agent: \(state.rawValue)")
            case .agentAudioReceived: heardAudio = true; log("non-silent agent audio received")
            case .transcript(let item) where item.isFinal && item.speaker == .agent:
                transcriptCount += 1
                log("agent transcript received (\(item.text.count) characters)")
            case .toolActivity(let activity): log("tool: \(activity.toolName), \(activity.status)")
            case .authentication(let auth): log("authentication: \(auth.scope), \(auth.status)")
            case .failure(let error): log(error.localizedDescription)
            default: break
            }
        }
        do {
            try await call.start(.init(businessSlug: business, agentPublicId: agent, endUserContact: contact),
                                 microphoneEnabled: false)
            let deadline = Date().addingTimeInterval(45)
            // The agent briefly reports listening before its protected greeting starts.
            while Date() < deadline, transcriptCount == 0 || call.agentState != .listening {
                if call.state == .failed { break }
                try await Task.sleep(for: .milliseconds(200))
            }
            guard transcriptCount > 0, call.agentState == .listening else { throw CallError.agentUnavailable }
            try await call.sendText("Hello. Please reply with one short sentence confirming you can hear from me. Do not call any banking tools.")
            let replyDeadline = Date().addingTimeInterval(30)
            while Date() < replyDeadline, transcriptCount < 2 || !heardAudio {
                if call.state == .failed { break }
                try await Task.sleep(for: .milliseconds(200))
            }
            let success = transcriptCount >= 2 && heardAudio
            await call.end()
            guard success else { throw CallError.connectionFailed }
            log("PASS: native call, agent greeting, chat response, transcript and non-silent audio. No microphone or voice-auth verification performed.")
        } catch {
            await call.end()
            throw error
        }
    }
}
