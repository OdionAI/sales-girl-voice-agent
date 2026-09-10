import AttentiveVoiceUI
import Foundation
import SwiftUI

@main
struct AttentiveExampleApp: App {
    private let environment = ProcessInfo.processInfo.environment
    @State private var showingCall = false

    var body: some Scene {
        WindowGroup {
            Button("Start call", systemImage: "phone") { showingCall = true }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("startCall")
                .fullScreenCover(isPresented: $showingCall) {
                    AttentiveAgentView(
                        apiKey: environment["ATTENTIVE_API_KEY"] ?? "YOUR_API_KEY",
                        agentID: environment["ATTENTIVE_AGENT_ID"] ?? "YOUR_AGENT_ID",
                        customerID: environment["ATTENTIVE_CUSTOMER_ID"],
                        callerToken: environment["ATTENTIVE_CALLER_TOKEN"].map { token in { token } }
                    )
                }
        }
    }
}
