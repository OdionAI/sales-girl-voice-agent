// Internal simulator-only staging test. Never packaged in the customer SDK.
#if DEBUG && os(iOS)
import AttentiveVoice
import AttentiveVoiceUI
import Combine
import Foundation
import SwiftUI

@main
struct AnimatedThemePublicStagingApp: App {
    @StateObject private var model = PublicStagingModel()

    var body: some Scene {
        WindowGroup {
            if let theme = model.theme {
                AttentiveCallerView(call: model.call, request: $model.request,
                    configuration: .init(title: "Talk to MoMo", agentName: "Amaka",
                                         showsToolWaitSelection: false, themeKey: theme),
                    microphoneOnStart: false, showsContactForm: true)
                    .task { await model.start() }
            } else {
                Text(model.error ?? "Loading agent")
                    .task { await model.loadTheme() }
            }
        }
    }
}

// This uses the same public-agent bootstrap as the staging browser, not the
// key-based SDK configuration endpoint. It is an internal integration test only.
private struct StagingCredentials: CallCredentialProvider {
    let provider: HTTPCallCredentialProvider
    let shaderReadiness: ShaderReadiness

    func fetch(for request: CallRequest) async throws -> CallCredentials {
        // The SDK has already entered connecting and mounted the existing avatar.
        // Do not create a server room until the real WebGL renderer is ready.
        try await shaderReadiness.wait()
        print("STAGING_SHADER_READY=true")
        let credentials = try await provider.fetch(for: request)
        guard credentials.serverUrl.scheme?.lowercased() == "wss",
              credentials.serverUrl.host?.lowercased() == "rtc.83.101.48.66.sslip.io" else {
            print("STAGING_RESULT=unexpected_media_destination")
            throw CallError.insecureURL
        }
        print("STAGING_MEDIA=expected_staging_host")
        return credentials
    }
}

private actor ShaderReadiness {
    private var ready = false
    func markReady() { ready = true }

    func wait() async throws {
        for _ in 0..<400 {
            try Task.checkCancellation()
            if ready { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        print("STAGING_RESULT=shader_timeout")
        throw CallError.serviceUnavailable
    }
}

@MainActor
private final class PublicStagingModel: ObservableObject {
    @Published var theme: String?
    @Published var error: String?
    @Published var request = CallRequest(businessSlug: "momo-psb", agentPublicId: "agt_3b5c72fdf2",
        endUserContact: "sdk-theme-\(UUID().uuidString.lowercased())@example.invalid")
    let call: AttentiveCall
    private var hasStarted = false
    private var heardAudio = false
    private var finished = false
    private var lastSampleTime: TimeInterval = 0
    private var subscriptions = Set<AnyCancellable>()

    init() {
        let endpoint = URL(string: "https://salesgirlstaging.odion.ai/api/public-agent/rvc-session")!
        let provider = try! HTTPCallCredentialProvider(endpoint: endpoint)
        let shaderReadiness = ShaderReadiness()
        call = AttentiveCall(credentialProvider: StagingCredentials(provider: provider, shaderReadiness: shaderReadiness))
        NotificationCenter.default.publisher(for: Notification.Name("AttentiveVoiceUI.ReferenceShaderStatus"))
            .receive(on: RunLoop.main)
            .sink { note in
                let rendered = note.userInfo?["rendered"] as? Bool ?? false
                let mode = note.userInfo?["mode"] as? String ?? "unknown"
                let intensity = note.userInfo?["intensity"] as? Double ?? 0
                let paused = note.userInfo?["paused"] as? Bool ?? true
                print(String(format: "STAGING_SHADER epoch=%.3f rendered=%@ mode=%@ intensity=%.3f paused=%@",
                             Date().timeIntervalSince1970, String(rendered), mode, intensity, String(paused)))
                if rendered && !paused { Task { await shaderReadiness.markReady() } }
            }.store(in: &subscriptions)
        call.agentAudioLevel.$energy.sink { [weak self] energy in
            guard let self else { return }
            let now = ProcessInfo.processInfo.systemUptime
            guard energy == 0 || now - self.lastSampleTime >= 0.2 else { return }
            self.lastSampleTime = now
            let speaking = self.call.state == .connected && self.call.agentState == .speaking
            let gated = speaking && energy.isFinite && energy > 0.01 ? min(1, max(0, energy)) : 0
            print(String(format: "STAGING_AUDIO epoch=%.3f uptime=%.3f state=%@ level=%.3f gated=%.3f", Date().timeIntervalSince1970, now,
                         self.call.agentState.rawValue, energy, gated))
        }.store(in: &subscriptions)
        call.onEvent = { [weak self] event in
            guard let self else { return }
            switch event {
            case .stateChanged(let state):
                print("STAGING_CALL_STATE=\(state.rawValue)")
                print("STAGING_EVENT epoch=\(Date().timeIntervalSince1970) call=\(state.rawValue)")
            case .agentStateChanged(let state):
                print("STAGING_AGENT_STATE=\(state.rawValue)")
                print("STAGING_EVENT epoch=\(Date().timeIntervalSince1970) agent=\(state.rawValue)")
            case .microphoneChanged(let enabled): print("STAGING_MICROPHONE=\(enabled ? "on" : "off")")
            case .toolActivity:
                print("STAGING_RESULT=unexpected_tool_activity")
                Task { @MainActor [weak self] in await self?.finish(result: "unexpected_tool_activity") }
            case .agentAudioEnergy(let energy) where energy > 0.01:
                guard !self.heardAudio else { return }
                self.heardAudio = true
                print("STAGING_ASSISTANT_OUTPUT=non_silent")
                Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(15))
                    await self?.finish(result: "completed")
                }
            case .failure: print("STAGING_RESULT=call_failed")
            default: break
            }
        }
    }

    func loadTheme() async {
        guard ProcessInfo.processInfo.environment["ATTENTIVE_STAGING_VERIFIED"] == "yes" else {
            error = "Staging verification required"
            print("STAGING_RESULT=verification_required")
            return
        }
        do {
            let url = URL(string: "https://salesgirlstaging.odion.ai/api/public-agent/resolve?businessSlug=momo-psb&agentPublicId=agt_3b5c72fdf2")!
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  http.url?.host?.lowercased() == "salesgirlstaging.odion.ai" else { throw CallError.invalidResponse }
            let agent = try JSONDecoder().decode(PublicAgent.self, from: data)
            guard agent.agent_public_id == "agt_3b5c72fdf2",
                  agent.default_language.lowercased() == "en",
                  agent.agent_theme_key.range(of: #"^animated:#[0-9A-Fa-f]{6}:(grain|smooth)$"#, options: .regularExpression) != nil else {
                throw CallError.invalidResponse
            }
            theme = agent.agent_theme_key
            print("STAGING_THEME=\(agent.agent_theme_key)")
            print("STAGING_LANGUAGE=en")
        } catch {
            self.error = "Could not load the staging agent"
            print("STAGING_RESULT=theme_load_failed")
        }
    }

    func start() async {
        guard !hasStarted else { return }
        hasStarted = true
        print("STAGING_MICROPHONE=off")
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(75))
            await self?.finish(result: "timeout")
        }
        do { try await call.start(request, microphoneEnabled: false) }
        catch {
            self.error = "Could not start the staging call"
            await finish(result: "call_failed")
        }
    }

    private func finish(result: String) async {
        guard !finished else { return }
        finished = true
        await call.end()
        print("STAGING_RESULT=\(result)")
        exit(result == "completed" ? EXIT_SUCCESS : EXIT_FAILURE)
    }
}

private struct PublicAgent: Decodable {
    let agent_public_id: String
    let agent_theme_key: String
    let default_language: String
}
#endif
