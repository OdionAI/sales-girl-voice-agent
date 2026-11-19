#if os(iOS)
import AttentiveVoice
import SwiftUI

/// The color scheme of the presented caller.
public enum AttentiveAppearance: Sendable {
    /// Use the host app's effective color scheme.
    case system
    case light
    case dark

    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

/// A complete dashboard-configured caller. No host model or call lifecycle code is needed.
/// Configure audio input with `audioInput` and speaking voice with `voiceLanguage` / `voiceGender`.
/// Preferences are code-only; the caller never displays settings controls.
@available(iOS 17, *)
@MainActor
public struct AttentiveAgentView: View {
    @StateObject private var call: AttentiveCall
    @State private var request = CallRequest(businessSlug: "", agentPublicId: "", endUserContact: "")
    @State private var error: String?
    @State private var loading = false
    @Environment(\.dismiss) private var dismiss
    private let callerToken: (() async throws -> String)?
    private let startsAutomatically: Bool
    private let microphoneOnStart: Bool
    private let appearance: AttentiveAppearance

    /// For protected calls, obtain a fresh caller token from your signed-in app backend.
    public init(apiKey: String, agentID: String, phoneNumber: String? = nil, audioInput: AttentiveAudioInputConfiguration = .standard,
                passkeys: AttentivePasskeyConfiguration = .disabled,
                voiceLanguage: VoiceAccent? = nil, voiceGender: VoiceGender? = nil,
                callerToken: (() async throws -> String)? = nil) {
        self.init(apiKey: apiKey, agentID: agentID, customerID: nil, phoneNumber: phoneNumber, audioInput: audioInput, passkeys: passkeys,
                  voiceLanguage: voiceLanguage, voiceGender: voiceGender, callerToken: callerToken)
    }

    /// Pass the signed-in customer's ID. The backend verifies it against callerToken before using it for tools.
    public init(apiKey: String, agentID: String, customerID: String?, phoneNumber: String? = nil,
                audioInput: AttentiveAudioInputConfiguration = .standard,
                passkeys: AttentivePasskeyConfiguration = .disabled,
                voiceLanguage: VoiceAccent? = nil, voiceGender: VoiceGender? = nil,
                callerToken: (() async throws -> String)? = nil) {
        self.init(apiKey: apiKey, agentID: agentID, customerID: customerID, phoneNumber: phoneNumber,
                  startsAutomatically: true, audioInput: audioInput, passkeys: passkeys,
                  voiceLanguage: voiceLanguage, voiceGender: voiceGender, callerToken: callerToken)
    }

    /// Present in a sheet or full-screen cover. Set false to show a reusable Start call button.
    public init(apiKey: String, agentID: String, customerID: String? = nil, phoneNumber: String? = nil,
                startsAutomatically: Bool, audioInput: AttentiveAudioInputConfiguration = .standard,
                passkeys: AttentivePasskeyConfiguration = .disabled,
                voiceLanguage: VoiceAccent? = nil, voiceGender: VoiceGender? = nil,
                callerToken: (() async throws -> String)? = nil) {
        self.init(apiKey: apiKey, agentID: agentID, customerID: customerID, phoneNumber: phoneNumber,
                  startsAutomatically: startsAutomatically, appearance: .system, audioInput: audioInput,
                  passkeys: passkeys, voiceLanguage: voiceLanguage, voiceGender: voiceGender, callerToken: callerToken)
    }

    /// Set appearance when presenting the caller in a sheet or full-screen cover.
    public init(apiKey: String, agentID: String, customerID: String? = nil, phoneNumber: String? = nil,
                startsAutomatically: Bool = true, appearance: AttentiveAppearance,
                audioInput: AttentiveAudioInputConfiguration = .standard,
                passkeys: AttentivePasskeyConfiguration = .disabled,
                voiceLanguage: VoiceAccent? = nil, voiceGender: VoiceGender? = nil,
                callerToken: (() async throws -> String)? = nil) {
        _call = StateObject(wrappedValue: AttentiveCall(apiKey: apiKey, agentID: agentID,
                                                      customerID: customerID, phoneNumber: phoneNumber, audioInput: audioInput, passkeys: passkeys,
                                                      voiceLanguage: voiceLanguage, voiceGender: voiceGender))
        self.callerToken = callerToken
        self.startsAutomatically = startsAutomatically
        self.microphoneOnStart = true
        self.appearance = appearance
    }

    #if DEBUG
    // Used only by the internal staging integration harness.
    init(stagingCall: AttentiveCall, startsAutomatically: Bool = true, appearance: AttentiveAppearance = .system,
         callerToken: (() async throws -> String)? = nil) {
        _call = StateObject(wrappedValue: stagingCall)
        self.callerToken = callerToken
        self.startsAutomatically = startsAutomatically
        self.microphoneOnStart = false
        self.appearance = appearance
    }
    #endif

    public var body: some View {
        Group {
            if let metadata = call.configuration {
                CallerContent(call: call, request: $request,
                    configuration: .init(title: metadata.title, agentName: metadata.agentName,
                                         showsToolWaitSelection: false, themeKey: metadata.themeKey),
                    enrollment: nil, microphoneOnStart: microphoneOnStart, onSettings: nil,
                    startsAutomatically: startsAutomatically,
                    startCall: {
                        if metadata.callerRequired && callerToken == nil { throw CallError.callerRequired }
                        let token = try await callerToken?()
                        try Task.checkCancellation()
                        try await call.start(callerToken: token, microphoneEnabled: microphoneOnStart)
                    })
            } else {
                VStack(spacing: 16) {
                    if loading { ProgressView().accessibilityLabel("Loading agent") }
                    if let error {
                        Text(error).multilineTextAlignment(.center)
                        Button("Try again", systemImage: "arrow.clockwise") { Task { await load() } }
                            .disabled(loading)
                    }
                }
                .padding(24).frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(CallerTheme.stage).foregroundStyle(CallerTheme.ink)
                .overlay(alignment: .topTrailing) {
                    CallerCloseButton { dismiss() }
                }
            }
        }
        .background(PasskeyWindowReader { [weak call] window in
            call?.passkeyPresentationAnchor = { [weak window] in window }
        })
        .preferredColorScheme(appearance.colorScheme)
        .task { if call.configuration == nil { await load() } }
    }

    private func load() async {
        guard !loading else { return }
        loading = true
        error = nil
        defer { loading = false }
        do { try await call.loadConfiguration() }
        catch is CancellationError {}
        catch { self.error = (error as? CallError ?? .connectionFailed).localizedDescription }
    }
}

@available(iOS 17, *)
struct CallerCloseButton: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Label("Close call window", systemImage: "xmark")
                .labelStyle(.iconOnly)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(Color.accentColor)
        .padding(16)
        .accessibilityIdentifier("cancelCall")
    }
}

/// Obtains the presenting scene without exposing a window parameter to SDK-view customers.
private struct PasskeyWindowReader: UIViewRepresentable {
    let update: (UIWindow?) -> Void
    func makeUIView(context: Context) -> WindowProbe { WindowProbe(update: update) }
    func updateUIView(_ view: WindowProbe, context: Context) { view.update = update }
    static func dismantleUIView(_ view: WindowProbe, coordinator: ()) { view.update(nil) }

    final class WindowProbe: UIView {
        var update: (UIWindow?) -> Void
        init(update: @escaping (UIWindow?) -> Void) {
            self.update = update
            super.init(frame: .zero)
            isUserInteractionEnabled = false
        }
        required init?(coder: NSCoder) { nil }
        override func didMoveToWindow() { super.didMoveToWindow(); update(window) }
    }
}
#endif
