import AttentiveVoice
import SwiftUI

/// Developer connection configuration belongs to the sample, not the caller library.
struct CallerSettings: View {
    @ObservedObject var model: SampleModel
    @ObservedObject private var enrollment: VoiceEnrollment
    @State private var config: SampleConfiguration
    @Environment(\.dismiss) private var dismiss

    init(model: SampleModel) {
        self.model = model
        enrollment = model.enrollment
        _config = State(initialValue: model.configuration)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Caller") {
                    field("Email or phone", text: $config.contact).keyboardType(.emailAddress)
                    field("Customer ID", text: $config.customerID)
                    field("Phone number", text: $config.phone).keyboardType(.phonePad)
                    field("Account number", text: $config.account).keyboardType(.numberPad)
                }
                Section("Call") {
                    Toggle("Microphone on start", isOn: $config.microphoneOnStart)
                    Picker("Waiting phrases", selection: $config.waitMode) {
                        Text("Tool specific").tag(ToolWaitSpeechMode.toolSpecific)
                        Text("LLM generated").tag(ToolWaitSpeechMode.llmGenerated)
                    }
                }
                Section("Connection") {
                    field("Call endpoint", text: $config.endpoint).keyboardType(.URL)
                    field("Business slug", text: $config.business)
                    field("Agent ID", text: $config.agent)
                }
            }
            .disabled(model.active || enrollment.isBusy).navigationTitle("Call settings").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { model.apply(config); if model.errorMessage == nil { dismiss() } }
                        .disabled(model.active || enrollment.isBusy).accessibilityIdentifier("saveSettings")
                }
            }
        }
    }

    private func field(_ label: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            TextField(label, text: text).textInputAutocapitalization(.never).autocorrectionDisabled()
        }.padding(.vertical, 3)
    }
}
