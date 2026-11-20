#if os(iOS)
import Foundation
import AttentiveVoice
import SwiftUI
import UIKit
import WebKit
import XCTest
@testable import AttentiveVoiceUI

@MainActor
final class ReferenceShaderWebKitTests: XCTestCase {
    func testStartScreenWarmsSelectedAnimationWithoutStartingCall() async throws {
        let rendered = expectation(description: "Selected shader is prepared on the start screen")
        rendered.assertForOverFulfill = false
        let observer = NotificationCenter.default.addObserver(
            forName: Notification.Name("AttentiveVoiceUI.ReferenceShaderStatus"), object: nil, queue: .main
        ) { note in
            if note.userInfo?["rendered"] as? Bool == true { rendered.fulfill() }
        }
        defer { NotificationCenter.default.removeObserver(observer) }

        let call = try AttentiveCall(endpoint: URL(string: "https://example.invalid/call")!)
        let request = CallRequest(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "test@example.invalid")
        let view = CallerContent(call: call, request: .constant(request),
            configuration: .init(themeKey: "animated:#EC653F:smooth"), enrollment: nil,
            microphoneOnStart: false, onSettings: nil, startCall: { XCTFail("Call started before the user requested it") })
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = UIHostingController(rootView: view)
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        await fulfillment(of: [rendered], timeout: 25)
        XCTAssertEqual(call.state, .idle)
    }

    func testAutomaticCallStartsAfterSelectedAnimationIsReady() async throws {
        let rendered = expectation(description: "Selected shader renders before the call")
        rendered.assertForOverFulfill = false
        let started = expectation(description: "Automatic call starts")
        let observer = NotificationCenter.default.addObserver(
            forName: Notification.Name("AttentiveVoiceUI.ReferenceShaderStatus"), object: nil, queue: .main
        ) { note in
            if note.userInfo?["rendered"] as? Bool == true,
               note.userInfo?["mode"] as? String == "listening" { rendered.fulfill() }
        }
        defer { NotificationCenter.default.removeObserver(observer) }

        let call = try AttentiveCall(endpoint: URL(string: "https://example.invalid/call")!)
        let request = CallRequest(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "test@example.invalid")
        let view = CallerContent(call: call, request: .constant(request),
            configuration: .init(themeKey: "animated:#2F6FED:grain"), enrollment: nil,
            microphoneOnStart: false, onSettings: nil, startsAutomatically: true,
            startCall: { started.fulfill() })
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = UIHostingController(rootView: view)
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        await fulfillment(of: [rendered, started], timeout: 25, enforceOrder: true)
    }

    func testBundledReferenceLoadsWebGLAndAppliesRealBridgeState() async throws {
        let script = try XCTUnwrap(ReferenceShaderView.referenceScript(), "The original shader and fallback assets must be bundled")
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentEnd, forMainFrameOnly: true))

        let webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 204, height: 204), configuration: configuration)
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
        try await Task.sleep(for: .milliseconds(500))
        let speakingScale = try await scale(webView)
        XCTAssertGreaterThan(speakingScale, 1.10)

        let listening = ReferenceShaderState(theme: theme, speaking: false, connected: true, energy: 0,
                                             reduceMotion: false, sceneActive: true, visible: true)
        _ = try await apply(listening, to: webView)
        let releaseScale = try await scale(webView)
        XCTAssertGreaterThan(releaseScale, 1.05, "Speech ending should start a smooth release")
        try await Task.sleep(for: .seconds(1))
        let restingScale = try await scale(webView)
        XCTAssertLessThan(restingScale, 1.02)

        let paused = ReferenceShaderState(theme: theme, speaking: true, connected: true, energy: 0.65,
                                          reduceMotion: false, sceneActive: false, visible: true)
        _ = try await apply(paused, to: webView)
        let inactive = try await snapshot(webView)
        XCTAssertEqual(inactive["mode"] as? String, "listening")
        XCTAssertEqual(inactive["intensity"] as? Double, 0)
        XCTAssertEqual(inactive["paused"] as? Bool, true)
        let pausedScale = try await scale(webView)
        XCTAssertEqual(pausedScale, 1, accuracy: 0.001)
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

    private func scale(_ webView: WKWebView) async throws -> Double {
        let value = try await webView.evaluateJavaScript("""
            (() => { const circle = document.getElementById('avatar');
              return circle.getBoundingClientRect().width / circle.clientWidth; })()
            """)
        return try XCTUnwrap(value as? Double)
    }

    private final class NavigationDelegate: NSObject, WKNavigationDelegate {
        let loaded: XCTestExpectation
        init(loaded: XCTestExpectation) { self.loaded = loaded }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { loaded.fulfill() }
    }
}
#endif
