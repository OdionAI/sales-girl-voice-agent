import AttentiveVoiceUI
import SwiftUI

@main
struct AttentiveExampleApp: App {
    var body: some Scene {
        WindowGroup {
            AttentiveAgentView(
                apiKey: "YOUR_API_KEY",
                agentID: "YOUR_AGENT_ID"
            )
        }
    }
}
