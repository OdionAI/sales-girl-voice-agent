// Internal Debug-only integration harness. Not included in the runtime SDK/package.
#if DEBUG && os(iOS)
@testable import AttentiveVoice
import AttentiveVoiceUI
import CryptoKit
import SwiftUI
import UIKit

@main
struct PasskeySimulatorApp: App {
    @StateObject private var model = SimulatorBankModel()
    var body: some Scene { WindowGroup { SimulatorBankScreen(model: model) } }
}

@MainActor
private struct SimulatorBankScreen: View {
    @ObservedObject var model: SimulatorBankModel
    @ObservedObject var call: AttentiveCall
    @ObservedObject var device: SimulatorCredentialDevice

    init(model: SimulatorBankModel) {
        self.model = model; call = model.call; device = model.device
    }

    var body: some View {
        VStack(spacing: 8) {
            Text("SIMULATED BANK · TEST ONLY").font(.headline).foregroundStyle(.orange)
            Text("Local agent audio. Synthetic credentials. No money moves.").font(.caption)
            Text("Bank: \(model.bankStage)").font(.caption).accessibilityIdentifier("bankStage")
            Text("SDK action: \(call.actionAuthentication.rawValue)")
                .font(.subheadline.monospaced()).accessibilityIdentifier("actionStatus")
            AttentiveCallerView(call: call, request: $model.request,
                configuration: .init(title: "Simulated bank", agentName: "Test assistant",
                                     showsToolWaitSelection: false), microphoneOnStart: false)
            HStack {
                Button("Approve path") { model.begin("approve") }.accessibilityIdentifier("approvePath")
                Button("Reject path") { model.begin("reject") }.accessibilityIdentifier("rejectPath")
                Button("Native check") { model.begin("native") }.accessibilityIdentifier("nativePath")
            }.buttonStyle(.bordered).font(.caption)
                .disabled(call.state != .connected || device.showingPrompt)
        }
        .background(SimulatorWindowReader { [weak device] in device?.window = $0 })
        .sheet(isPresented: $device.showingPrompt, onDismiss: { device.cancel() }) {
            VStack(spacing: 24) {
                Text("SIMULATED PASSKEY").font(.title2.bold())
                Text("This is a test credential prompt, not Face ID or Wema authentication.")
                    .multilineTextAlignment(.center)
                Text("Approve a synthetic signature for this local test action?")
                Button("Approve simulated passkey") { device.approve() }
                    .buttonStyle(.borderedProminent).accessibilityIdentifier("approveSimulatedPasskey")
                Button("Cancel simulated passkey", role: .cancel) { device.cancel() }
                    .accessibilityIdentifier("cancelSimulatedPasskey")
            }.padding(32).presentationDetents([.medium])
        }
        .task { await model.observeBank() }
    }
}

@MainActor
private final class SimulatorBankModel: ObservableObject {
    let device: SimulatorCredentialDevice
    let call: AttentiveCall
    @Published var bankStage = "Start the local call"
    @Published var request = CallRequest(businessSlug: "simulated-bank", agentPublicId: "agt_simulated", endUserContact: "simulator@example.test")
    private let base = URL(string: "http://127.0.0.1:8765")!

    init() {
        let device = SimulatorCredentialDevice()
        self.device = device
        call = AttentiveCall(provider: SimulatorBootstrap(publicKey: device.key.publicKey.derRepresentation),
            allowInsecure: true, makeTransport: { RealtimeCallTransport() }, microphonePermission: { true },
            passkeys: .init(relyingPartyIdentifiers: ["auth.bank.example"]), passkeyAuthorizer: device)
        call.onEvent = { event in
            switch event {
            case .stateChanged(let state): print("SIMULATOR_CALL_STATE \(state.rawValue)")
            case .authentication(let value): print("SIMULATOR_AUTH \(value.status.rawValue)")
            case .agentAudioReceived: print("SIMULATOR_AGENT_AUDIO_RECEIVED")
            case .failure(let error): print("SIMULATOR_CALL_ERROR \(error.localizedDescription)")
            default: break
            }
        }
    }

    func begin(_ mode: String) {
        device.useNative = mode == "native"
        Task {
            do {
                var request = URLRequest(url: base.appendingPathComponent("scenario"))
                request.httpMethod = "POST"
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(["mode": mode])
                let (_, response) = try await URLSession.shared.data(for: request)
                if (response as? HTTPURLResponse)?.statusCode != 200 { bankStage = "Scenario not ready" }
            } catch { bankStage = "Local test server unavailable" }
        }
    }

    func observeBank() async {
        while !Task.isCancelled {
            do {
                let (data, _) = try await URLSession.shared.data(from: base.appendingPathComponent("status"))
                let status = try JSONDecoder().decode(BankStatus.self, from: data)
                bankStage = status.stage
            } catch { bankStage = "Local test server unavailable" }
            do { try await Task.sleep(for: .milliseconds(500)) } catch { return }
        }
    }

    private struct BankStatus: Decodable { let stage: String }
}

private struct SimulatorBootstrap: CallCredentialProvider {
    let publicKey: Data
    func fetch(for request: CallRequest) async throws -> CallCredentials {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8765/start")!)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["publicKey": publicKey.base64EncodedString()])
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw CallError.serviceUnavailable }
        return try JSONDecoder().decode(CallCredentials.self, from: data)
    }
}

@MainActor
private final class SimulatorCredentialDevice: ObservableObject, PasskeyAuthorizing {
    let key = P256.Signing.PrivateKey()
    @Published var showingPrompt = false
    weak var window: UIWindow?
    var useNative = false
    private var pending: PasskeyRequest?
    private var continuation: CheckedContinuation<PasskeyProof, Error>?
    private lazy var native = NativePasskeyAuthorizer { [weak self] in self?.window }

    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof {
        if useNative { return try await native.authorize(request) }
        pending = request
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            showingPrompt = true
        }
    }

    func approve() {
        guard let request = pending, let continuation else { return }
        do {
            let clientData = try JSONEncoder().encode(["type": "webauthn.get", "challenge": PasskeyEncoding.encode(request.challenge), "origin": "https://auth.bank.example"])
            let authenticatorData = Data(SHA256.hash(data: Data(request.relyingParty.utf8))) + Data([5, 0, 0, 0, 1])
            let signature = try key.signature(for: authenticatorData + Data(SHA256.hash(data: clientData))).derRepresentation
            let proof = PasskeyProof.assertion(id: Data(SHA256.hash(data: key.publicKey.rawRepresentation)),
                clientData: clientData, authenticatorData: authenticatorData, signature: signature, userID: Data("simulated-user".utf8))
            self.continuation = nil; pending = nil; showingPrompt = false
            continuation.resume(returning: proof)
        } catch {
            self.continuation = nil; pending = nil; showingPrompt = false
            continuation.resume(throwing: PasskeyFailure.authorizationFailed)
        }
    }

    func cancel() {
        let continuation = continuation
        self.continuation = nil; pending = nil; showingPrompt = false
        native.cancel()
        continuation?.resume(throwing: PasskeyFailure.cancelled)
    }
}

private struct SimulatorWindowReader: UIViewRepresentable {
    let update: (UIWindow?) -> Void
    func makeUIView(context: Context) -> Probe { Probe(update: update) }
    func updateUIView(_ view: Probe, context: Context) { view.update = update }
    final class Probe: UIView {
        var update: (UIWindow?) -> Void
        init(update: @escaping (UIWindow?) -> Void) { self.update = update; super.init(frame: .zero); isUserInteractionEnabled = false }
        required init?(coder: NSCoder) { nil }
        override func didMoveToWindow() { super.didMoveToWindow(); update(window) }
    }
}
#else
#error("The simulated bank harness is for Debug iOS builds only.")
#endif
