import AttentiveVoice
import AttentiveVoiceUI
import SwiftUI

/// Customer-style composition: the caller surface comes entirely from the UI product.
struct CallScreen: View {
    @ObservedObject var model: SampleModel
    @ObservedObject var call: AttentiveCall
    @State private var showSettings = false

    var body: some View {
        AttentiveCallerView(call: call, request: $model.configuration.request,
                            configuration: model.appearance,
                            enrollment: ProcessInfo.processInfo.arguments.contains("--generic-ui") ? nil : model.enrollment,
                            microphoneOnStart: model.configuration.microphoneOnStart,
                            onSettings: { showSettings = true })
            .sheet(isPresented: $showSettings) { CallerSettings(model: model) }
            .alert("Invalid configuration", isPresented: Binding(
                get: { model.errorMessage != nil },
                set: { if !$0 { model.errorMessage = nil } }
            )) {
                Button("OK", role: .cancel) { model.errorMessage = nil }
            } message: { Text(model.errorMessage ?? "") }
    }
}
