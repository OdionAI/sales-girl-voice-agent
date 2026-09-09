#if os(iOS)
import AttentiveVoice
import SwiftUI

/// A complete dashboard-configured caller. No host model or call lifecycle code is needed.
@available(iOS 17, *)
@MainActor
public struct AttentiveAgentView: View {
    @StateObject private var call: AttentiveCall
    @State private var request = CallRequest(businessSlug: "", agentPublicId: "", endUserContact: "")
    @State private var error: String?
    @State private var loading = false
    private let callerToken: (() async throws -> String)?

    /// For protected calls, obtain a fresh caller token from your signed-in app backend.
    public init(apiKey: String, agentID: String, callerToken: (() async throws -> String)? = nil) {
        _call = StateObject(wrappedValue: AttentiveCall(apiKey: apiKey, agentID: agentID))
        self.callerToken = callerToken
    }

    public var body: some View {
        Group {
            if let metadata = call.configuration {
                CallerContent(call: call, request: $request,
                    configuration: .init(title: metadata.title, agentName: metadata.agentName,
                                         showsToolWaitSelection: false),
                    enrollment: nil, microphoneOnStart: true, onSettings: nil,
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
            }
        }
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
#endif
