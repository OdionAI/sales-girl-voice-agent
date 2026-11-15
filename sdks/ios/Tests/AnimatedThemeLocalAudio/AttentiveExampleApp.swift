// Local-only debug integration test; not packaged in the SDK.
#if DEBUG && os(iOS)
import AttentiveVoice
import AttentiveVoiceUI
import Combine
import CryptoKit
import Foundation
import SwiftUI

@main
struct AnimatedThemeLocalAudioApp: App {
    @StateObject private var model = LocalAudioModel()

    var body: some Scene {
        WindowGroup {
            AttentiveCallerView(call: model.call, request: $model.request,
                configuration: .init(title: "Local audio test", agentName: "Test assistant",
                                     showsToolWaitSelection: false, themeKey: "animated:#2F6FED:grain"),
                microphoneOnStart: false)
                .overlay(alignment: .top) { Text("LOCAL TEST · NO STAGING CALL").font(.caption.bold()) }
                .task { await model.start() }
        }
    }
}

private struct LocalCredentials: CallCredentialProvider {
    func fetch(for request: CallRequest) async throws -> CallCredentials {
        let key = P256.Signing.PrivateKey()
        var http = URLRequest(url: URL(string: "http://127.0.0.1:18766/start")!)
        http.httpMethod = "POST"
        http.setValue("application/json", forHTTPHeaderField: "Content-Type")
        http.httpBody = try JSONEncoder().encode(["publicKey": key.publicKey.derRepresentation.base64EncodedString()])
        let (data, response) = try await URLSession.shared.data(for: http)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw CallError.serviceUnavailable }
        let credentials = try JSONDecoder().decode(CallCredentials.self, from: data)
        guard credentials.serverUrl.host == "127.0.0.1" else { throw CallError.insecureURL }
        return credentials
    }
}

@MainActor
private final class LocalAudioModel: ObservableObject {
    let call = AttentiveCall(credentialProvider: LocalCredentials(), allowsInsecureDevelopmentConnections: true)
    @Published var request = CallRequest(businessSlug: "local-test", agentPublicId: "agt_local_test", endUserContact: "local@example.invalid")
    private var started = false
    private var heardAudio = false
    private var samples = Set<AnyCancellable>()

    init() {
        NotificationCenter.default.publisher(for: Notification.Name("AttentiveVoiceUI.ReferenceShaderStatus"))
            .receive(on: RunLoop.main)
            .sink { note in
                let rendered = note.userInfo?["rendered"] as? Bool ?? false
                let mode = note.userInfo?["mode"] as? String ?? "unknown"
                let intensity = note.userInfo?["intensity"] as? Double ?? 0
                let paused = note.userInfo?["paused"] as? Bool ?? true
                print(String(format: "LOCAL_SHADER epoch=%.3f rendered=%@ mode=%@ intensity=%.3f paused=%@",
                             Date().timeIntervalSince1970, String(rendered), mode, intensity, String(paused)))
            }.store(in: &samples)
        call.agentAudioLevel.$energy.sink { [weak self] energy in
            guard let self else { return }
            let speaking = self.call.state == .connected && self.call.agentState == .speaking
            let gated = speaking && energy.isFinite && energy > 0.01 ? min(1, max(0, energy)) : 0
            print(String(format: "LOCAL_AUDIO epoch=%.3f state=%@ level=%.3f gated=%.3f",
                         Date().timeIntervalSince1970, self.call.agentState.rawValue, energy, gated))
        }.store(in: &samples)
        call.onEvent = { [weak self] event in
            guard let self else { return }
            switch event {
            case .stateChanged(let state): print("LOCAL_STATE epoch=\(Date().timeIntervalSince1970) call=\(state.rawValue)")
            case .agentStateChanged(let state): print("LOCAL_STATE epoch=\(Date().timeIntervalSince1970) agent=\(state.rawValue)")
            case .agentAudioEnergy(let energy) where energy > 0.01:
                guard !self.heardAudio else { return }
                self.heardAudio = true
                print("LOCAL_AUDIO_RECEIVED=non_silent")
                Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(12))
                    await self?.finish()
                }
            case .failure: print("LOCAL_RESULT=call_failed")
            default: break
            }
        }
    }

    func start() async {
        guard !started else { return }
        started = true
        do { try await call.start(request, microphoneEnabled: false) }
        catch { print("LOCAL_RESULT=start_failed") }
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(40))
            await self?.finish()
        }
    }

    private func finish() async {
        guard call.state != .ended else { return }
        await call.end()
        print("LOCAL_RESULT=\(heardAudio ? "completed" : "silent")")
        exit(heardAudio ? EXIT_SUCCESS : EXIT_FAILURE)
    }
}
#endif
