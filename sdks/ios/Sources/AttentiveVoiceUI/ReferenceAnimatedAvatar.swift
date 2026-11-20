#if os(iOS)
import SwiftUI
import WebKit

// Hosts the original WebGL renderer locally. The call and its audio stay native;
// only the palette, texture, state and scalar output level enter the web view.
struct ReferenceAnimatedAvatar: View {
    let theme: AnimatedAgentTheme
    let speaking: Bool
    let connected: Bool
    let energy: Float
    let reduceMotion: Bool
    let sceneActive: Bool
    @Binding var ready: Bool
    @State private var visible = false
    @State private var rendererFailed = false

    var body: some View {
        let state = ReferenceShaderState(theme: theme, speaking: speaking, connected: connected,
                                         energy: energy, reduceMotion: reduceMotion,
                                         sceneActive: sceneActive, visible: visible)
        ZStack {
            if !ready { ProgressView().tint(CallerTheme.muted) }
            if rendererFailed {
                LinearGradient(colors: theme.palette.map { value in
                    Color(.sRGB, red: Double((value >> 16) & 255) / 255,
                          green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
                }, startPoint: .topLeading, endPoint: .bottomTrailing)
                    .frame(width: CallerTheme.avatarDiameter, height: CallerTheme.avatarDiameter)
                    .clipShape(Circle())
            }
            ReferenceShaderView(state: state, onReady: { available in
                rendererFailed = !available
                ready = true
            })
                .opacity(ready && !rendererFailed ? 1 : 0)
        }
            .onAppear { visible = true }
            .onDisappear { visible = false; ready = false; rendererFailed = false }
            .task {
                try? await Task.sleep(for: .seconds(15))
                guard !Task.isCancelled, !ready else { return }
                rendererFailed = true
                ready = true
            }
    }
}

struct ReferenceShaderView: UIViewRepresentable {
    let state: ReferenceShaderState
    let onReady: (Bool) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onReady: onReady) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        if let script = Self.referenceScript() {
            configuration.userContentController.addUserScript(
                WKUserScript(source: script, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        } else {
            DispatchQueue.main.async { context.coordinator.reportReady(false) }
        }
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.isOpaque = false
        view.backgroundColor = .clear
        view.scrollView.backgroundColor = .clear
        view.scrollView.isScrollEnabled = false
        view.isUserInteractionEnabled = false
        context.coordinator.pending = state
        view.loadHTMLString(Self.document, baseURL: nil)
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        context.coordinator.onReady = onReady
        context.coordinator.apply(state, to: view)
    }

    static func dismantleUIView(_ view: WKWebView, coordinator: Coordinator) {
        view.evaluateJavaScript("window.attentivePause && window.attentivePause()", completionHandler: nil)
        view.stopLoading()
        view.navigationDelegate = nil
    }

    static let document = """
    <!doctype html><html><head>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
    <style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}
    body{display:grid;place-items:center}
    blue-circle{display:block;width:\(CallerTheme.avatarDiameter)px;height:\(CallerTheme.avatarDiameter)px;will-change:transform}</style>
    </head><body><blue-circle id="avatar"></blue-circle></body></html>
    """

    static func referenceScript() -> String? {
        guard let jsURL = Bundle.module.url(forResource: "blue-circle", withExtension: "js"),
              let pngURL = Bundle.module.url(forResource: "fallback", withExtension: "png"),
              let source = try? String(contentsOf: jsURL, encoding: .utf8),
              let image = try? Data(contentsOf: pngURL),
              source.contains("export class BlueCircle"),
              source.contains("new URL('./fallback.png', import.meta.url)") else { return nil }
        // Load the shader module and fallback locally without network access.
        let bundled = source
            .replacingOccurrences(of: "export class BlueCircle", with: "class BlueCircle")
            .replacingOccurrences(of: "new URL('./fallback.png', import.meta.url)",
                                  with: "'data:image/png;base64,\(image.base64EncodedString())'")
        return bundled + """

        window.attentiveSetAnimation = function(state) {
          const circle = document.getElementById('avatar');
          if (!circle) return;
          const wasPaused = !!circle.nativePaused;
          circle.nativePaused = !!state.paused;
          const colors = state.colors.join(',');
          if (circle.getAttribute('colors') !== colors) circle.setAttribute('colors', colors);
          if (circle.getAttribute('texture') !== state.texture) circle.setAttribute('texture', state.texture);
          if (circle.getAttribute('mode') !== state.mode) circle.setAttribute('mode', state.mode);
          circle.intensity = state.intensity;
          if (wasPaused !== circle.nativePaused && circle.motion && typeof circle.update === 'function') circle.update();
          return { rendered: !!(circle.gl && circle.program && circle.canvas && circle.canvas.style.visibility === 'visible'),
                   mode: state.mode, intensity: state.intensity, paused: state.paused };
        };
        window.attentivePause = function() {
          const circle = document.getElementById('avatar');
          if (circle) { circle.nativePaused = true; circle.intensity = 0; if (circle.motion && circle.update) circle.update(); }
        };
        """
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var pending: ReferenceShaderState?
        var onReady: (Bool) -> Void
        private var ready = false
        private var reportedReady = false
        private var lastSent: ReferenceShaderState?

        init(onReady: @escaping (Bool) -> Void) { self.onReady = onReady }

        func reportReady(_ available: Bool) {
            guard !reportedReady else { return }
            reportedReady = true
            onReady(available)
        }

        func apply(_ state: ReferenceShaderState, to view: WKWebView) {
            pending = state
            guard ready, state != lastSent, let encoded = try? JSONEncoder().encode(state),
                  let json = String(data: encoded, encoding: .utf8) else { return }
            lastSent = state
            let script = "window.attentiveSetAnimation && window.attentiveSetAnimation(\(json))"
            view.evaluateJavaScript(script) { [weak self] value, _ in
                self?.reportReady(value is [String: Any])
                #if DEBUG
                let info = value as? [String: Any] ?? ["rendered": false]
                NotificationCenter.default.post(name: Notification.Name("AttentiveVoiceUI.ReferenceShaderStatus"),
                                                object: nil, userInfo: info)
                #endif
            }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            ready = true
            if let pending { apply(pending, to: webView) }
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            reportReady(false)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            reportReady(false)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            onReady(false)
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            decisionHandler(navigationAction.request.url?.scheme == "about" ? .allow : .cancel)
        }
    }
}
#endif
