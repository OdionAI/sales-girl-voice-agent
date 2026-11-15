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
    @State private var visible = false

    var body: some View {
        let state = ReferenceShaderState(theme: theme, speaking: speaking, connected: connected,
                                         energy: energy, reduceMotion: reduceMotion,
                                         sceneActive: sceneActive, visible: visible)
        ReferenceShaderView(state: state)
            .background(Color(.sRGB, red: Double((theme.rgb >> 16) & 255) / 255,
                              green: Double((theme.rgb >> 8) & 255) / 255,
                              blue: Double(theme.rgb & 255) / 255))
            .onAppear { visible = true }
            .onDisappear { visible = false }
    }
}

struct ReferenceShaderView: UIViewRepresentable {
    let state: ReferenceShaderState

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        if let script = Self.referenceScript() {
            configuration.userContentController.addUserScript(
                WKUserScript(source: script, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
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
    blue-circle{display:block;width:100%;height:100%}</style>
    </head><body><blue-circle id="avatar"></blue-circle></body></html>
    """

    static func referenceScript() -> String? {
        guard let jsURL = Bundle.module.url(forResource: "blue-circle", withExtension: "js"),
              let pngURL = Bundle.module.url(forResource: "fallback", withExtension: "png"),
              let source = try? String(contentsOf: jsURL, encoding: .utf8),
              let image = try? Data(contentsOf: pngURL),
              source.contains("export class BlueCircle"),
              source.contains("new URL('./fallback.png', import.meta.url)"),
              source.contains("if (!this.visible || document.hidden || this.motion.matches) return;") else { return nil }
        // The GLSL and visual timing remain unchanged. These adaptations allow the
        // module and fallback image to run locally and honour native app lifecycle.
        let bundled = source
            .replacingOccurrences(of: "export class BlueCircle", with: "class BlueCircle")
            .replacingOccurrences(of: "new URL('./fallback.png', import.meta.url)",
                                  with: "'data:image/png;base64,\(image.base64EncodedString())'")
            .replacingOccurrences(of: "if (!this.visible || document.hidden || this.motion.matches) return;",
                                  with: "if (!this.visible || document.hidden || this.motion.matches || this.nativePaused) return;")
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
        private var ready = false
        private var lastSent: ReferenceShaderState?

        func apply(_ state: ReferenceShaderState, to view: WKWebView) {
            pending = state
            guard ready, state != lastSent, let encoded = try? JSONEncoder().encode(state),
                  let json = String(data: encoded, encoding: .utf8) else { return }
            lastSent = state
            let script = "window.attentiveSetAnimation && window.attentiveSetAnimation(\(json))"
            #if DEBUG
            view.evaluateJavaScript(script) { value, _ in
                let info = value as? [String: Any] ?? ["rendered": false]
                NotificationCenter.default.post(name: Notification.Name("AttentiveVoiceUI.ReferenceShaderStatus"),
                                                object: nil, userInfo: info)
            }
            #else
            view.evaluateJavaScript(script, completionHandler: nil)
            #endif
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            ready = true
            if let pending { apply(pending, to: webView) }
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            decisionHandler(navigationAction.request.url?.scheme == "about" ? .allow : .cancel)
        }
    }
}
#endif
