#if os(iOS)
import Foundation
import UIKit
import WebKit
import XCTest
@testable import AttentiveVoiceUI

@MainActor
final class ReferenceShaderWebKitTests: XCTestCase {
    func testBundledReferenceLoadsWebGLAndAppliesRealBridgeState() async throws {
        let script = try XCTUnwrap(ReferenceShaderView.referenceScript(), "The original shader and fallback assets must be bundled")
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentEnd, forMainFrameOnly: true))

        let webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 123, height: 123), configuration: configuration)
        let controller = UIViewController()
        controller.view.addSubview(webView)
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = controller
        window.makeKeyAndVisible()
        defer { window.isHidden = true; webView.stopLoading() }

        let loaded = expectation(description: "Local shader document loaded")
        let delegate = NavigationDelegate(loaded: loaded)
        webView.navigationDelegate = delegate
        webView.loadHTMLString(ReferenceShaderView.document, baseURL: nil)
        await fulfillment(of: [loaded], timeout: 20)

        let theme = try XCTUnwrap(AnimatedAgentTheme(key: "animated:#ab09ff:smooth"))
        let talking = ReferenceShaderState(theme: theme, speaking: true, connected: true, energy: 0.65,
                                           reduceMotion: false, sceneActive: true, visible: true)
        let talkingResult = try await apply(talking, to: webView)
        XCTAssertEqual(talkingResult["rendered"] as? Bool, true, "WebGL program must actually compile and render")
        let active = try await snapshot(webView)
        XCTAssertEqual(active["colors"] as? String, "#8907cc,#c85dff,#e2a9ff")
        XCTAssertEqual(active["texture"] as? String, "smooth")
        XCTAssertEqual(active["mode"] as? String, "talking")
        XCTAssertEqual(try XCTUnwrap(active["intensity"] as? Double), 0.65, accuracy: 0.0001)
        XCTAssertEqual(active["paused"] as? Bool, false)

        let paused = ReferenceShaderState(theme: theme, speaking: true, connected: true, energy: 0.65,
                                          reduceMotion: false, sceneActive: false, visible: true)
        _ = try await apply(paused, to: webView)
        let inactive = try await snapshot(webView)
        XCTAssertEqual(inactive["mode"] as? String, "listening")
        XCTAssertEqual(inactive["intensity"] as? Double, 0)
        XCTAssertEqual(inactive["paused"] as? Bool, true)
    }

    private func apply(_ state: ReferenceShaderState, to webView: WKWebView) async throws -> [String: Any] {
        let json = try XCTUnwrap(String(data: JSONEncoder().encode(state), encoding: .utf8))
        let value = try await webView.evaluateJavaScript("window.attentiveSetAnimation(\(json))")
        return try XCTUnwrap(value as? [String: Any])
    }

    private func snapshot(_ webView: WKWebView) async throws -> [String: Any] {
        let js = """
        (() => { const c = document.getElementById('avatar'); return {
          colors: c.getAttribute('colors'), texture: c.getAttribute('texture'),
          mode: c.getAttribute('mode'), intensity: c.intensity, paused: !!c.nativePaused
        }; })()
        """
        let value = try await webView.evaluateJavaScript(js)
        return try XCTUnwrap(value as? [String: Any])
    }

    private final class NavigationDelegate: NSObject, WKNavigationDelegate {
        let loaded: XCTestExpectation
        init(loaded: XCTestExpectation) { self.loaded = loaded }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { loaded.fulfill() }
    }
}
#endif
