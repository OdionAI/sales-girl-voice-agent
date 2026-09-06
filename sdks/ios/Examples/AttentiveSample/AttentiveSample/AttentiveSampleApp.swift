import SwiftUI

@main
struct AttentiveSampleApp: App {
    @StateObject private var model = SampleModel()

    var body: some Scene {
        WindowGroup {
            CallScreen(model: model, call: model.call)
                .tint(CallerTheme.accent)
                .preferredColorScheme(.light)
        }
    }
}
