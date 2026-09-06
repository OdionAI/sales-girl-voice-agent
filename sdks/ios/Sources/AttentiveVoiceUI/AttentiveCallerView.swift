#if os(iOS)
import AttentiveVoice
import SwiftUI

/// Optional caller UI. Retain the call in the host; do not replace it during a call.
@available(iOS 17, *)
@MainActor
public struct AttentiveCallerView: View {
    private let call: AttentiveCall
    private let enrollment: VoiceEnrollment?
    private let configuration: CallerUIConfiguration
    private let onSettings: (() -> Void)?
    @Binding private var request: CallRequest
    private let microphoneOnStart: Bool

    public init(call: AttentiveCall, request: Binding<CallRequest>,
                configuration: CallerUIConfiguration = .init(),
                enrollment: VoiceEnrollment? = nil,
                microphoneOnStart: Bool = true,
                onSettings: (() -> Void)? = nil) {
        self.call = call
        self.enrollment = enrollment
        self.configuration = configuration
        self.onSettings = onSettings
        _request = request
        self.microphoneOnStart = microphoneOnStart
    }

    public var body: some View {
        CallerContent(call: call, request: $request, configuration: configuration,
                      enrollment: enrollment, microphoneOnStart: microphoneOnStart,
                      onSettings: onSettings)
            .id(ContentIdentity(call: ObjectIdentifier(call), enrollment: enrollment.map(ObjectIdentifier.init)))
    }

    private struct ContentIdentity: Hashable {
        let call: ObjectIdentifier
        let enrollment: ObjectIdentifier?
    }
}

@available(iOS 17, *)
@MainActor
private struct CallerContent: View {
    @ObservedObject var call: AttentiveCall
    @StateObject private var controls: CallerControls
    @Binding var request: CallRequest
    let configuration: CallerUIConfiguration
    let microphoneOnStart: Bool
    let onSettings: (() -> Void)?
    @State private var panel: Panel?
    @State private var draft = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase

    private enum Panel { case bank, transcript }

    init(call: AttentiveCall, request: Binding<CallRequest>, configuration: CallerUIConfiguration,
         enrollment: VoiceEnrollment?, microphoneOnStart: Bool, onSettings: (() -> Void)?) {
        self.call = call
        _request = request
        self.configuration = configuration
        self.microphoneOnStart = microphoneOnStart
        self.onSettings = onSettings
        _controls = StateObject(wrappedValue: CallerControls(call: call, enrollment: enrollment))
    }

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
                    if controls.active {
                        VStack(spacing: 22) {
                            CallerAvatar(speaking: call.agentState == .speaking,
                                         image: configuration.avatar ?? Image("CallerAvatar", bundle: .module),
                                         audioLevel: call.agentAudioLevel)
                                .accessibilityElement(children: .ignore)
                                .accessibilityLabel("\(configuration.agentName), \(status)")
                                .accessibilityIdentifier("callerAvatar")
                            Text(status).font(.system(size: 13)).foregroundStyle(CallerTheme.muted)
                                .accessibilityIdentifier("callStatus")
                        }.offset(y: -16)
                    } else {
                        ScrollView {
                            StartCallCard(contact: $request.endUserContact, enrollment: controls.enrollment,
                                          enrollmentBusy: controls.enrollmentBusy, configuration: configuration) {
                                if request.endUserContact.contains("@") {
                                    request.endUserContact = request.endUserContact
                                        .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                                }
                                let next = request
                                Task { await controls.start(next, microphoneEnabled: microphoneOnStart) }
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
                            CallerProfilePanel(controls: controls, call: call, request: $request,
                                configuration: configuration,
                                openSettings: onSettings.map { action in { closePanel(); action() } },
                                openTranscript: { togglePanel(.transcript) })
                        } else {
                            CallerTranscriptPanel(controls: controls, call: call, status: status,
                                                  agentName: configuration.agentName, draft: $draft)
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
                .accessibilityLabel(panel == nil ? "Open \(configuration.profileTitle)" : "Close floating panel")
                .accessibilityIdentifier("bankMenu").padding(.top, 8).padding(.trailing, 16)
            }
        }
        .foregroundStyle(CallerTheme.ink)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if controls.active { callDock.padding(.top, 12).padding(.bottom, 20) }
        }
        .background(CallerTheme.stage)
        .tint(configuration.accentColor)
        .onAppear { controls.appear() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background { controls.enrollment?.cancel() }
        }
        .onDisappear { Task { await controls.disappear(endsCall: configuration.endsCallOnDisappear) } }
        .alert("Call unavailable", isPresented: Binding(
            get: { controls.errorMessage != nil }, set: { if !$0 { controls.errorMessage = nil } }
        )) {
            Button("OK", role: .cancel) { controls.errorMessage = nil }
        } message: { Text(controls.errorMessage ?? "") }
    }

    private var callDock: some View {
        HStack(spacing: 6) {
            Button { Task { await controls.toggleMicrophone() } } label: {
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
                Button(configuration.profileTitle, systemImage: "person.crop.circle") { togglePanel(.bank) }
                if let onSettings {
                    Button("Call settings", systemImage: "slider.horizontal.3", action: onSettings)
                }
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 18)).frame(width: 44, height: 44)
                    .background(CallerTheme.control, in: Circle())
            }.tint(CallerTheme.ink).accessibilityLabel("More call options")
            Button { Task { await controls.end() } } label: {
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

@available(iOS 17, *)
private struct StartCallCard: View {
    @Binding var contact: String
    let enrollment: VoiceEnrollment?
    let enrollmentBusy: Bool
    let configuration: CallerUIConfiguration
    var start: () -> Void
    var body: some View {
        VStack(spacing: 18) {
            Text(configuration.title).font(.system(size: 16, weight: .medium))
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
                    .disabled(enrollmentBusy)
                if let enrollment {
                    VoiceEnrollmentControl(enrollment: enrollment, email: contact, accent: configuration.accentColor)
                }
                Button(action: start) {
                    Label("Start call", systemImage: "phone").font(.system(size: 14, weight: .medium))
                        .frame(maxWidth: .infinity, minHeight: 48)
                        .background(configuration.accentColor, in: Capsule()).foregroundStyle(.white)
                }
                .buttonStyle(.plain).accessibilityIdentifier("startCall")
                .disabled(enrollmentBusy || contact.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(22).frame(maxWidth: 340)
        .background(CallerTheme.surface, in: RoundedRectangle(cornerRadius: 28))
        .overlay(RoundedRectangle(cornerRadius: 28).stroke(CallerTheme.border, lineWidth: 1))
        .shadow(color: CallerTheme.ink.opacity(0.08), radius: 30, x: 0, y: 20)
    }
}

@available(iOS 17, *)
private struct VoiceEnrollmentControl: View {
    @ObservedObject var enrollment: VoiceEnrollment
    let email: String
    let accent: Color

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
                    .tint(accent).accessibilityLabel("Voice recording progress")
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

#endif
