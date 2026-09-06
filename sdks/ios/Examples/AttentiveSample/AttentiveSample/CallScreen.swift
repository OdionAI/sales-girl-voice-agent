import AttentiveVoice
import SwiftUI

struct CallScreen: View {
    @ObservedObject var model: SampleModel
    @ObservedObject var call: AttentiveCall
    @State private var panel: Panel?
    @State private var showSettings = false
    @State private var draft = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase

    private enum Panel { case bank, transcript }

    private var status: String {
        switch call.state {
        case .idle: return "Ready to call"
        case .connecting: return "Calling..."
        case .reconnecting: return "Reconnecting..."
        case .ending: return "Ending call..."
        case .ended: return "Call ended"
        case .failed: return "Unable to connect"
        case .connected:
            switch call.agentState {
            case .listening: return "Listening"
            case .thinking: return "Thinking"
            case .speaking: return "Speaking"
            default: return "Ringing..."
            }
        }
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .topTrailing) {
                CallerTheme.stage.ignoresSafeArea()
                VStack {
                    Spacer(minLength: 12)
                    if model.active {
                        VStack(spacing: 22) {
                            CallerAvatar(speaking: call.agentState == .speaking, audioLevel: call.agentAudioLevel)
                                .accessibilityElement(children: .ignore)
                                .accessibilityLabel("SAW, \(status)")
                                .accessibilityIdentifier("callerAvatar")
                            Text(status).font(.system(size: 13)).foregroundStyle(CallerTheme.muted)
                                .accessibilityIdentifier("callStatus")
                        }.offset(y: -16)
                    } else {
                        ScrollView {
                            StartCallCard(contact: $model.configuration.contact, enrollment: model.enrollment) {
                                Task { await model.start() }
                            }
                            .padding(.horizontal, 24).padding(.vertical, 20)
                            .frame(maxWidth: .infinity)
                            .frame(minHeight: max(0, geometry.size.height - 24))
                        }.scrollBounceBehavior(.basedOnSize).scrollDismissesKeyboard(.interactively)
                    }
                    Spacer(minLength: 12)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityHidden(panel != nil)

                if panel != nil {
                    Button { closePanel() } label: { Color.clear.contentShape(Rectangle()) }
                        .buttonStyle(.plain).accessibilityLabel("Dismiss floating panel")
                        .accessibilityIdentifier("dismissPanel")
                    Group {
                        if panel == .bank {
                            WemaCallerPanel(model: model, call: call,
                                openSettings: { closePanel(); showSettings = true },
                                openTranscript: { togglePanel(.transcript) })
                        } else {
                            CallerTranscriptPanel(model: model, call: call, status: status, draft: $draft)
                        }
                    }
                    .frame(width: min(390, max(0, geometry.size.width - 24)))
                    .frame(height: min(max(0, geometry.size.height - 68),
                                       panel == .bank && call.toolActivity.isEmpty ? 550 : 760), alignment: .top)
                    .background(.white).clipShape(RoundedRectangle(cornerRadius: 14))
                    .overlay(RoundedRectangle(cornerRadius: 14).stroke(CallerTheme.border, lineWidth: 1))
                    .shadow(color: CallerTheme.ink.opacity(0.14), radius: 20, x: 0, y: 18)
                    .padding(.top, 64).padding(.trailing, 12)
                    .transition(.opacity.combined(with: .offset(y: -8)))
                }

                Button {
                    withAnimation(reduceMotion ? nil : .easeOut(duration: 0.18)) { panel = panel == nil ? .bank : nil }
                } label: {
                    Image(systemName: panel == nil ? "line.3.horizontal" : "xmark")
                        .font(.system(size: 18)).contentTransition(.symbolEffect(.replace))
                        .frame(width: 44, height: 44).background(.white, in: Circle())
                        .overlay(Circle().stroke(CallerTheme.border, lineWidth: 1))
                        .shadow(color: CallerTheme.ink.opacity(0.08), radius: 12, x: 0, y: 6)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(panel == nil ? "Open My Wema" : "Close floating panel")
                .accessibilityIdentifier("bankMenu").padding(.top, 8).padding(.trailing, 16)
            }
        }
        .foregroundStyle(CallerTheme.ink)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if model.active { callDock.padding(.top, 12).padding(.bottom, 20) }
        }
        .background(CallerTheme.stage)
        .sheet(isPresented: $showSettings) { CallerSettings(model: model) }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background { model.enrollment.cancel() }
        }
        .onDisappear { model.enrollment.cancel() }
        .alert("Call unavailable", isPresented: Binding(
            get: { model.errorMessage != nil }, set: { if !$0 { model.errorMessage = nil } }
        )) {
            Button("OK", role: .cancel) { model.errorMessage = nil }
        } message: { Text(model.errorMessage ?? "") }
    }

    private var callDock: some View {
        HStack(spacing: 6) {
            Button { Task { await model.toggleMicrophone() } } label: {
                Image(systemName: call.microphoneEnabled ? "mic" : "mic.slash")
            }
            .buttonStyle(CallerIconStyle(tint: call.microphoneEnabled ? CallerTheme.ink : CallerTheme.red))
            .disabled(call.state != .connected)
            .accessibilityLabel(call.microphoneEnabled ? "Mute microphone" : "Unmute microphone")
            .accessibilityIdentifier("microphone")
            Button { togglePanel(.transcript) } label: { Image(systemName: "ellipsis.message") }
                .buttonStyle(CallerIconStyle(selected: panel == .transcript))
                .accessibilityLabel("Live transcript").accessibilityIdentifier("transcriptToggle")
            Menu {
                Button("My Wema", systemImage: "person.crop.circle") { togglePanel(.bank) }
                Button("Call settings", systemImage: "slider.horizontal.3") { showSettings = true }
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 18)).frame(width: 44, height: 44)
                    .background(CallerTheme.control, in: Circle())
            }.tint(CallerTheme.ink).accessibilityLabel("More call options")
            Button { Task { await model.end() } } label: {
                Label("END CALL", systemImage: "phone.down").font(.system(size: 12, weight: .medium))
                    .padding(.horizontal, 16).frame(minHeight: 44)
                    .background(CallerTheme.endFill, in: Capsule())
            }
            .buttonStyle(.plain).foregroundStyle(CallerTheme.endInk).disabled(call.state == .ending)
            .accessibilityLabel("End call").accessibilityIdentifier("endCall")
        }
        .padding(8).background(CallerTheme.surface, in: Capsule())
        .overlay(Capsule().stroke(CallerTheme.border, lineWidth: 1))
        .shadow(color: CallerTheme.ink.opacity(0.08), radius: 20, x: 0, y: 12)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func togglePanel(_ target: Panel) {
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.18)) { panel = panel == target ? nil : target }
    }
    private func closePanel() {
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.18)) { panel = nil }
    }
}

private struct StartCallCard: View {
    @Binding var contact: String
    @ObservedObject var enrollment: VoiceEnrollment
    var start: () -> Void
    var body: some View {
        VStack(spacing: 18) {
            Text("Talk to Wema Bank").font(.system(size: 16, weight: .medium))
            Text("Enter your email or phone number to start the call.")
                .font(.system(size: 13)).foregroundStyle(CallerTheme.muted)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 12) {
                TextField("Email or phone number", text: $contact)
                    .font(.system(size: 14)).keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .padding(.horizontal, 20).frame(minHeight: 48)
                    .background(CallerTheme.field, in: Capsule())
                    .overlay(Capsule().stroke(CallerTheme.border, lineWidth: 1))
                    .accessibilityIdentifier("callerContact")
                    .disabled(enrollment.isBusy)
                VoiceEnrollmentControl(enrollment: enrollment, email: contact)
                Button(action: start) {
                    Label("Start call", systemImage: "phone").font(.system(size: 14, weight: .medium))
                        .frame(maxWidth: .infinity, minHeight: 48)
                        .background(CallerTheme.accent, in: Capsule()).foregroundStyle(.white)
                }
                .buttonStyle(.plain).accessibilityIdentifier("startCall")
                .disabled(enrollment.isBusy || contact.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(22).frame(maxWidth: 340)
        .background(CallerTheme.surface, in: RoundedRectangle(cornerRadius: 28))
        .overlay(RoundedRectangle(cornerRadius: 28).stroke(CallerTheme.border, lineWidth: 1))
        .shadow(color: CallerTheme.ink.opacity(0.08), radius: 30, x: 0, y: 20)
    }
}

private struct VoiceEnrollmentControl: View {
    @ObservedObject var enrollment: VoiceEnrollment
    let email: String

    private var message: String {
        if let error = enrollment.lastError { return error.localizedDescription }
        switch enrollment.stage {
        case .checking: return "Checking voice enrollment..."
        case .recording: return "Keep speaking naturally. \(enrollment.remainingSeconds) seconds remaining."
        case .saving: return "Saving your voiceprint..."
        default: return enrollment.isEnrolled ? "Voice enrolled for this email." : "Record 8 seconds of your voice."
        }
    }

    var body: some View {
        VStack(spacing: 10) {
            Label("Voice enrollment", systemImage: enrollment.isEnrolled ? "checkmark.circle" : "waveform")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(enrollment.isEnrolled ? CallerTheme.green : CallerTheme.ink)
            Text(message).font(.system(size: 12))
                .foregroundStyle(enrollment.lastError == nil ? CallerTheme.muted : CallerTheme.red)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("enrollmentStatus")
            if enrollment.stage == .recording {
                ProgressView(value: Double(8 - enrollment.remainingSeconds), total: 8)
                    .tint(CallerTheme.accent).accessibilityLabel("Voice recording progress")
                Button { enrollment.cancel() } label: {
                    Label("Cancel recording", systemImage: "stop.circle")
                        .font(.system(size: 13)).frame(maxWidth: .infinity, minHeight: 44)
                }.buttonStyle(.plain).accessibilityIdentifier("cancelEnrollment")
            } else {
                Button { Task { await enrollment.record(email: email) } } label: {
                    HStack(spacing: 8) {
                        if enrollment.stage == .saving { ProgressView().controlSize(.small) }
                        else { Image(systemName: "mic") }
                        Text(enrollment.stage == .saving ? "Saving voice..." : enrollment.isEnrolled ? "Re-record my voice" : "Record my voice")
                    }.font(.system(size: 13, weight: .medium)).frame(maxWidth: .infinity, minHeight: 44)
                        .background(.white, in: Capsule())
                        .overlay(Capsule().stroke(CallerTheme.border, lineWidth: 1))
                }.buttonStyle(.plain).disabled(enrollment.isBusy || enrollment.stage == .checking)
                    .accessibilityIdentifier("recordVoice")
            }
        }
        .padding(.vertical, 6)
        .task(id: email) { await enrollment.refresh(email: email) }
    }
}

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
