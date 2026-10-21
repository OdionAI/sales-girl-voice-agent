#if os(iOS)
import AttentiveVoice
import SwiftUI

/// A complete dashboard-configured caller. No host model or call lifecycle code is needed.
/// Configure audio input with `audioInput`; the caller never displays audio settings.
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

    /// For protected calls, obtain a fresh caller token from your signed-in app backend.
    public init(apiKey: String, agentID: String, audioInput: AttentiveAudioInputConfiguration = .standard,
                passkeys: AttentivePasskeyConfiguration = .disabled,
                callerToken: (() async throws -> String)? = nil) {
        self.init(apiKey: apiKey, agentID: agentID, customerID: nil, audioInput: audioInput, passkeys: passkeys, callerToken: callerToken)
    }

    /// Pass the signed-in customer's ID. The backend verifies it against callerToken before using it for tools.
    public init(apiKey: String, agentID: String, customerID: String?,
                audioInput: AttentiveAudioInputConfiguration = .standard,
                passkeys: AttentivePasskeyConfiguration = .disabled,
                callerToken: (() async throws -> String)? = nil) {
        self.init(apiKey: apiKey, agentID: agentID, customerID: customerID,
                  startsAutomatically: true, audioInput: audioInput, passkeys: passkeys, callerToken: callerToken)
    }

    /// Present from a host-owned button. Set false to retain the built-in Start call button.
    public init(apiKey: String, agentID: String, customerID: String? = nil,
                startsAutomatically: Bool, audioInput: AttentiveAudioInputConfiguration = .standard,
                passkeys: AttentivePasskeyConfiguration = .disabled,
                callerToken: (() async throws -> String)? = nil) {
        _call = StateObject(wrappedValue: AttentiveCall(apiKey: apiKey, agentID: agentID,
                                                      customerID: customerID, audioInput: audioInput, passkeys: passkeys))
        self.callerToken = callerToken
        self.startsAutomatically = startsAutomatically
    }

    public var body: some View {
        Group {
            if let metadata = call.configuration {
                CallerContent(call: call, request: $request,
                    configuration: .init(title: metadata.title, agentName: metadata.agentName,
                                         showsToolWaitSelection: false, themeKey: metadata.themeKey),
                    enrollment: nil, microphoneOnStart: true, onSettings: nil,
                    startsAutomatically: startsAutomatically, onCallEnded: { dismiss() },
                    startCall: {
                        if metadata.callerRequired && callerToken == nil { throw CallError.callerRequired }
                        let token = try await callerToken?()
                        try Task.checkCancellation()
                        try await call.start(callerToken: token)
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
                    Button("Close", systemImage: "xmark") { dismiss() }
                        .labelStyle(.iconOnly).frame(width: 44, height: 44).padding(16)
                        .accessibilityIdentifier("cancelCall")
                }
            }
        }
        .background(PasskeyWindowReader { [weak call] window in
            call?.passkeyPresentationAnchor = { [weak window] in window }
        })
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
