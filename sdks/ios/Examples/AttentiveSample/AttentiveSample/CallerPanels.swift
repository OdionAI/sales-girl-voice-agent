import AttentiveVoice
import SwiftUI

struct WemaCallerPanel: View {
    @ObservedObject var model: SampleModel
    @ObservedObject var call: AttentiveCall
    var openSettings: () -> Void
    var openTranscript: () -> Void

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("My Wema").font(.system(size: 16, weight: .semibold))
                        Text(model.active ? "Profile locked during call" : "Caller profile")
                            .font(.system(size: 12)).foregroundStyle(CallerTheme.muted)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    VStack(alignment: .trailing, spacing: 6) {
                        AuthBadge(label: "Session", status: call.sessionAuthentication)
                        AuthBadge(label: "Action", status: call.actionAuthentication)
                    }
                }.padding(18)
                divider
                VStack(alignment: .leading, spacing: 18) {
                    profileField("Wema customer ID", text: $model.configuration.customerID)
                    if model.configuration.account.isEmpty {
                        profileField("Phone number", text: $model.configuration.phone, keyboard: .phonePad)
                    } else {
                        HStack(alignment: .top, spacing: 12) {
                            profileField("Account number", text: $model.configuration.account, keyboard: .numberPad)
                            profileField("Phone number", text: $model.configuration.phone, keyboard: .phonePad)
                        }
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Speech during tool calls").font(.system(size: 12)).foregroundStyle(CallerTheme.muted)
                        Picker("Speech during tool calls", selection: $model.configuration.waitMode) {
                            Text("Tool specific").tag(ToolWaitSpeechMode.toolSpecific)
                            Text("LLM generated").tag(ToolWaitSpeechMode.llmGenerated)
                        }.pickerStyle(.segmented).disabled(model.active)
                    }
                }.padding(18)
                divider
                HStack {
                    Text("Bank activity").font(.system(size: 14, weight: .semibold))
                    Spacer()
                    if !call.toolActivity.isEmpty {
                        Text("\(call.toolActivity.count)").font(.system(size: 12)).foregroundStyle(CallerTheme.muted)
                    }
                }.padding(18)
                divider
                if call.toolActivity.isEmpty {
                    Text("No bank activity yet").font(.system(size: 14)).foregroundStyle(CallerTheme.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 44)
                }
                LazyVStack(spacing: 0) {
                    ForEach(call.toolActivity.reversed()) { item in
                        ToolActivityRow(item: item)
                        divider
                    }
                }
                if !call.transcripts.isEmpty {
                    Button(action: openTranscript) {
                        Label("View transcript", systemImage: "ellipsis.message")
                            .font(.system(size: 12)).frame(maxWidth: .infinity, minHeight: 44)
                    }.buttonStyle(.plain).accessibilityIdentifier("viewTranscript")
                }
                Button(action: openSettings) {
                    Label("Call settings", systemImage: "slider.horizontal.3")
                        .font(.system(size: 12)).frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.plain).foregroundStyle(CallerTheme.muted)
                .accessibilityIdentifier("callerDetails")
            }
        }
        .scrollBounceBehavior(.basedOnSize).scrollDismissesKeyboard(.interactively)
    }

    private var divider: some View { Rectangle().fill(CallerTheme.divider).frame(height: 1) }

    private func profileField(_ label: String, text: Binding<String>, keyboard: UIKeyboardType = .default) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(label).font(.system(size: 12)).foregroundStyle(CallerTheme.muted)
            TextField(label, text: text)
                .font(.system(size: 14)).keyboardType(keyboard)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
                .padding(.horizontal, 12).frame(minHeight: 44)
                .background(model.active ? CallerTheme.field : .white, in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(CallerTheme.border, lineWidth: 1))
                .disabled(model.active)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct AuthBadge: View {
    let label: String
    let status: AuthenticationStatus.Status
    private var tint: Color {
        status == .verified ? CallerTheme.green : status == .failed ? CallerTheme.red : CallerTheme.muted
    }
    var body: some View {
        Label(label, systemImage: status == .verified ? "checkmark.shield" : status == .failed ? "xmark.shield" : "shield")
            .font(.system(size: 10, weight: .medium)).foregroundStyle(tint)
            .padding(.horizontal, 7).padding(.vertical, 4)
            .background(tint.opacity(0.08), in: Capsule())
            .accessibilityLabel("\(label) authentication: \(status.rawValue)")
    }
}

private struct ToolActivityRow: View {
    let item: ToolActivity
    @State private var expanded = false

    private var status: String { item.status.isEmpty ? item.event : item.status }
    private var running: Bool { ["running", "started", "start"].contains(status) }
    private var tint: Color {
        if running { return CallerTheme.accent }
        if ["failed", "error", "blocked"].contains(status) { return CallerTheme.red }
        if status == "needs_input" { return .orange }
        if ["ok", "success", "completed", "complete", "prepared"].contains(status) { return CallerTheme.green }
        return CallerTheme.muted
    }
    private var statusLabel: String {
        switch status {
        case "running", "started", "start": return "Calling"
        case "ok", "success", "completed", "complete": return "Complete"
        default: return status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
    private var name: String {
        let names = ["wema_get_balance": "Check balance", "wema_get_transactions": "Transaction history",
                     "wema_prepare_transfer": "Prepare transfer", "wema_prepare_airtime": "Prepare airtime",
                     "wema_prepare_data_purchase": "Prepare data purchase", "wema_list_data_plans": "Find data plans",
                     "wema_execute_prepared": "Execute prepared request", "wema_list_transfer_banks": "Find transfer bank"]
        return names[item.toolName] ?? item.toolName.replacingOccurrences(of: "wema_", with: "")
            .replacingOccurrences(of: "_", with: " ").capitalized
    }
    private var result: String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return (try? encoder.encode(item.payload)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }

    var body: some View {
        VStack(spacing: 0) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 12) {
                    if running { ProgressView().tint(tint).controlSize(.mini).frame(width: 10) }
                    else { Circle().fill(tint).frame(width: 9, height: 9) }
                    VStack(alignment: .leading, spacing: 5) {
                        Text(name).font(.system(size: 14, weight: .medium))
                        Text(statusLabel).font(.system(size: 12)).foregroundStyle(CallerTheme.muted)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 10)).foregroundStyle(CallerTheme.muted)
                }.padding(18).contentShape(Rectangle())
            }
            .buttonStyle(.plain).accessibilityValue(statusLabel)
            if expanded {
                VStack(alignment: .leading, spacing: 10) {
                    Text(running ? "REQUEST" : "RESULT").font(.system(size: 10, weight: .medium))
                        .foregroundStyle(CallerTheme.muted)
                    ScrollView {
                        Text(result).font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(CallerTheme.muted).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }.frame(maxHeight: 192)
                }.padding(18).background(CallerTheme.stage)
            }
        }.background(running ? CallerTheme.accent.opacity(0.06) : .clear)
    }
}

struct CallerTranscriptPanel: View {
    @ObservedObject var model: SampleModel
    @ObservedObject var call: AttentiveCall
    let status: String
    @Binding var draft: String
    @FocusState private var composing: Bool

    private var readyToSend: Bool {
        call.state == .connected && [.listening, .thinking, .speaking].contains(call.agentState)
            && call.transcripts.contains { $0.speaker == .agent && $0.isFinal }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Live transcript").font(.system(size: 14, weight: .semibold))
                    Text(status).font(.system(size: 11)).foregroundStyle(CallerTheme.muted)
                        .accessibilityIdentifier("transcriptStatus")
                }
                Spacer()
                if model.receivedAudio {
                    Image(systemName: "speaker.wave.2").font(.system(size: 12))
                        .foregroundStyle(CallerTheme.green)
                        .accessibilityLabel("Audio connected").accessibilityIdentifier("audioReceived")
                }
            }.padding(18)
            Divider().overlay(CallerTheme.divider)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 20) {
                        if call.transcripts.isEmpty {
                            Text("No conversation yet").font(.system(size: 13))
                                .foregroundStyle(CallerTheme.muted).padding(.vertical, 36)
                        }
                        ForEach(call.transcripts) { item in TranscriptBubble(item: item).id(item.id) }
                        Color.clear.frame(height: 1).id("latest")
                    }.padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
                .onAppear { proxy.scrollTo("latest", anchor: .bottom) }
                .onChange(of: call.transcripts) { proxy.scrollTo("latest", anchor: .bottom) }
                .onChange(of: composing) { proxy.scrollTo("latest", anchor: .bottom) }
            }
            Divider().overlay(CallerTheme.divider)
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Message SAW", text: $draft, axis: .vertical)
                    .font(.system(size: 14)).lineLimit(1...3).focused($composing)
                    .padding(12).frame(minHeight: 44)
                    .background(CallerTheme.stage, in: RoundedRectangle(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).stroke(CallerTheme.border, lineWidth: 1))
                    .accessibilityIdentifier("chatInput")
                Button {
                    let text = draft
                    Task { if await model.send(text) { draft = ""; composing = false } }
                } label: {
                    Image(systemName: "arrow.up").font(.system(size: 16, weight: .medium))
                        .foregroundStyle(.white).frame(width: 44, height: 44)
                        .background(CallerTheme.ink.opacity(readyToSend && !draft.isEmpty ? 1 : 0.4), in: Circle())
                }
                .buttonStyle(.plain)
                .disabled(!readyToSend || model.sending || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel("Send message").accessibilityIdentifier("sendMessage")
            }.padding(12)
        }
    }
}

private struct TranscriptBubble: View {
    let item: Transcript
    var body: some View {
        HStack {
            if item.speaker == .caller { Spacer(minLength: 30) }
            Text(item.text).font(.system(size: 13)).lineSpacing(6)
                .foregroundStyle(item.isFinal ? CallerTheme.ink : CallerTheme.muted).textSelection(.enabled)
                .padding(item.speaker == .agent ? 14 : 0)
                .background(item.speaker == .agent ? CallerTheme.field.opacity(0.7) : .clear,
                            in: RoundedRectangle(cornerRadius: 20))
                .overlay(RoundedRectangle(cornerRadius: 20)
                    .stroke(item.speaker == .agent ? CallerTheme.divider : .clear, lineWidth: 1))
                .accessibilityIdentifier(item.speaker == .agent
                    ? (item.isFinal ? "agentTranscript" : "agentPartialTranscript") : "callerTranscript")
            if item.speaker == .agent { Spacer(minLength: 12) }
        }.frame(maxWidth: .infinity)
    }
}
