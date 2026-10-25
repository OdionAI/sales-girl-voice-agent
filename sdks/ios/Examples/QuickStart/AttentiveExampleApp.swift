import AttentiveVoice
import AttentiveVoiceUI
import Foundation
import SwiftUI

@main
struct AttentiveExampleApp: App {
    private let environment = ProcessInfo.processInfo.environment
    @State private var showingCall = false

    var body: some Scene {
        WindowGroup {
            VStack(spacing: 24) {
                Text("Attentive").font(.title.weight(.semibold))
                Button("Start call", systemImage: "phone") { showingCall = true }
                    .buttonStyle(.borderedProminent)
                    .accessibilityIdentifier("startCall")
            }
                .fullScreenCover(isPresented: $showingCall) {
                    AttentiveAgentView(
                        apiKey: environment["ATTENTIVE_API_KEY"] ?? "YOUR_API_KEY",
                        agentID: environment["ATTENTIVE_AGENT_ID"] ?? "YOUR_AGENT_ID",
                        customerID: environment["ATTENTIVE_CUSTOMER_ID"],
                        audioInput: .init(
                            noiseControl: .noisyEnvironment,
                            responseSensitivity: .balanced,
                            noiseSuppressionDB: -12,
                            minimumSpeechLevelDBFS: -50,
                            speechConfirmationMilliseconds: 120,
                            interruptionConfirmationMilliseconds: 240
                        ),
                        // Demo placeholders; replace with your bank-approved values.
                        // Native verification requires the bank's app/domain association and backend.
                        passkeys: .init(
                            relyingPartyIdentifiers: ["auth.bank.example"],
                            deviceIdentifier: "DEMO-DEVICE-ID"
                        ),
                        // Optional speaking voice; omit both to keep the service default.
                        voiceLanguage: .enNG,
                        voiceGender: .female,
                        callerToken: environment["ATTENTIVE_CALLER_TOKEN"].map { token in { token } }
                    )
                }
        }
    }
}
