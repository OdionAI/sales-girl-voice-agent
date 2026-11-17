// Internal Debug-only staging harness. It is never included in the SDK release.
#if DEBUG && os(iOS)
@testable import AttentiveVoice
@testable import AttentiveVoiceUI
import Combine
import Foundation
import SwiftUI

@main
struct AnimatedThemeStagingApp: App {
    @StateObject private var model = StagingModel()
    var body: some Scene {
        WindowGroup {
            if let call = model.call {
                AttentiveAgentView(stagingCall: call, startsAutomatically: !model.preview, callerToken: model.callerToken)
                    .overlay(alignment: .top) {
                        if model.preview {
                            Text("PREVIEW · NO CALL")
                                .font(.caption.bold()).padding(8).background(.regularMaterial, in: Capsule())
                        }
                    }
            } else {
                Text("Staging configuration unavailable")
            }
        }
    }
}

@MainActor
private final class StagingModel: ObservableObject {
    let call: AttentiveCall?
    let callerToken: (() async throws -> String)?
    let preview: Bool
    private var subscriptions = Set<AnyCancellable>()
    private var didObserveAudio = false

    init() {
        let env = ProcessInfo.processInfo.environment
        if let key = env["ATTENTIVE_THEME_PREVIEW"],
           key.range(of: #"^animated:#[0-9A-Fa-f]{6}:(grain|smooth)$"#, options: .regularExpression) != nil {
            let activeCall = AttentiveCall(animatedThemePreview: key)
            call = activeCall
            callerToken = nil
            preview = true
            print("PREVIEW_MODE=synthetic_no_call")
            Task { @MainActor [weak activeCall] in
                var phase: Float = 0
                while let activeCall {
                    activeCall.setAnimatedThemePreviewEnergy(0.2 + 0.45 * (1 + sin(phase)) / 2)
                    phase += 0.2
                    try? await Task.sleep(for: .milliseconds(50))
                }
            }
            return
        }
        preview = false
        guard env["ATTENTIVE_STAGING_VERIFIED"] == "yes",
              let address = env["ATTENTIVE_STAGING_API_BASE"],
              let expectedHost = env["ATTENTIVE_STAGING_CONFIRMED_HOST"]?.lowercased(),
              let url = URL(string: address), let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme == "https", let host = components.host?.lowercased(), host == expectedHost,
              host == "salesgirlstaging.odion.ai" || host.split(separator: ".").contains(where: { $0 == "staging" || $0.hasSuffix("-staging") || $0.hasPrefix("staging-") }),
              components.user == nil, components.password == nil, components.query == nil, components.fragment == nil,
              components.port == nil || components.port == 443,
              components.path == "/api/sdk/v1/", let apiKey = env["ATTENTIVE_STAGING_API_KEY"],
              let agentID = env["ATTENTIVE_STAGING_AGENT_ID"] else {
            call = nil; callerToken = nil
            print("STAGING_RESULT=configuration_rejected")
            return
        }

        let client = SDKClient(apiKey: apiKey, agentID: agentID,
                               customerID: env["ATTENTIVE_STAGING_CUSTOMER_ID"], baseURL: url)
        let activeCall = AttentiveCall(stagingClient: client)
        call = activeCall
        if let token = env["ATTENTIVE_STAGING_CALLER_TOKEN"], !token.isEmpty {
            callerToken = { token }
        } else { callerToken = nil }

        activeCall.$configuration.compactMap { $0 }.sink { configuration in
            let key = configuration.themeKey ?? "default"
            if key.range(of: #"^animated:#[0-9A-Fa-f]{6}:(grain|smooth)$"#, options: .regularExpression) != nil {
                print("STAGING_THEME=\(key)")
            } else { print("STAGING_THEME=not_animated") }
        }.store(in: &subscriptions)
        activeCall.onEvent = { [weak self, weak activeCall] event in
            switch event {
            case .stateChanged(let state): print("STAGING_CALL_STATE=\(state.rawValue)")
            case .agentStateChanged(let state): print("STAGING_AGENT_STATE=\(state.rawValue)")
            case .microphoneChanged(let enabled): print("STAGING_MICROPHONE=\(enabled ? "on" : "off")")
            case .agentAudioEnergy(let energy) where energy > 0.01:
                guard let self, !self.didObserveAudio else { return }
                self.didObserveAudio = true
                print("STAGING_ASSISTANT_OUTPUT=non_silent")
                // Give the tester time to observe the caller UI before clean shutdown.
                Task { @MainActor in
                    try? await Task.sleep(for: .seconds(15))
                    await activeCall?.end()
                    print("STAGING_RESULT=completed")
                    exit(EXIT_SUCCESS)
                }
            case .failure: print("STAGING_RESULT=call_failed")
            default: break
            }
        }
        Task { @MainActor [weak activeCall] in
            try? await Task.sleep(for: .seconds(75))
            await activeCall?.end()
            print("STAGING_RESULT=timeout")
            exit(EXIT_FAILURE)
        }
    }
}
#endif
