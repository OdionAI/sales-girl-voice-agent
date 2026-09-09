import XCTest

final class SampleUITests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    func testInsufficientCreditAlert() throws {
        guard let endpoint = ProcessInfo.processInfo.environment["ATTENTIVE_CREDIT_TEST_ENDPOINT"] else {
            throw XCTSkip("Requires a local fixture returning HTTP 402 with code no_airtime.")
        }
        let app = XCUIApplication()
        app.launchArguments = ["--chat-only", "--generic-ui"]
        app.launchEnvironment["ATTENTIVE_CALL_ENDPOINT"] = endpoint
        app.launchEnvironment["ATTENTIVE_LOCAL_DEVICE"] = "0"
        app.launchEnvironment["ATTENTIVE_CALLER_CONTACT"] = "credit-ui-test@example.com"
        app.launch()
        XCTAssertTrue(app.buttons["startCall"].waitForExistence(timeout: 10))
        app.buttons["startCall"].tap()
        let alert = app.alerts["Call credit needed"]
        XCTAssertTrue(alert.waitForExistence(timeout: 15))
        XCTAssertTrue(alert.staticTexts.matching(NSPredicate(format: "label == %@",
            "This agent's account does not have enough call credit. Please ask the account owner to top up in the Attentive dashboard, then try again.")).firstMatch.exists)
        XCTAssertFalse(alert.staticTexts.containing(NSPredicate(format: "label CONTAINS '402'")).firstMatch.exists)
        attachScreen("Insufficient call credit notification")
        alert.buttons["OK"].tap()
        XCTAssertFalse(alert.exists)
        XCTAssertTrue(app.buttons["startCall"].isEnabled)
    }

    func testPublicDefaultsUseWemaWithoutUnavailableEnrollment() {
        let app = XCUIApplication()
        app.launchEnvironment["ATTENTIVE_LOCAL_DEVICE"] = "0"
        app.launch()
        XCTAssertTrue(app.buttons["startCall"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["recordVoice"].exists)
        XCTAssertFalse(app.alerts["Invalid configuration"].exists)
        app.buttons["bankMenu"].tap()
        XCTAssertTrue(app.staticTexts["Bank activity"].exists)
        app.buttons["callerDetails"].tap()
        XCTAssertEqual(app.textFields["Call endpoint"].value as? String,
                       "https://attentive.odion.ai/api/public-agent/connection-details")
        XCTAssertEqual(app.textFields["Business slug"].value as? String, "wema-bank-poc-local")
        XCTAssertEqual(app.textFields["Agent ID"].value as? String, "agt_73099afb71")
        attachScreen("Public Lagos Wema configuration")
    }

    func testLocalLaunchRoutingSurvivesRelaunchWithoutSavingCallerData() {
        let suite = "attentive.local-launch-test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let routing = ["ATTENTIVE_CALL_ENDPOINT": "http://192.168.1.5:3004/api/public-agent/rvc-session",
                       "ATTENTIVE_BUSINESS_SLUG": "wema-bank-poc-local",
                       "ATTENTIVE_AGENT_ID": "comparison-agent", "ATTENTIVE_LOCAL_DEVICE": "1"]
        let launch = routing.merging(["ATTENTIVE_CALLER_CONTACT": "private@example.com",
                                     "ATTENTIVE_CUSTOMER_ID": "private", "SERVICE_TOKEN": "secret"]) { _, new in new }
        XCTAssertEqual(SampleLocalLaunchSettings.environment(launch, defaults: defaults), launch)
        let restored = SampleLocalLaunchSettings.environment([:], defaults: defaults)
        #if DEBUG
        XCTAssertEqual(restored, routing)
        XCTAssertTrue(LocalDeviceConnectionPolicy(endpoint: URL(string: routing["ATTENTIVE_CALL_ENDPOINT"]!)!,
                                                 environment: restored).enabled)
        #else
        XCTAssertTrue(restored.isEmpty)
        #endif
        XCTAssertNil(restored["ATTENTIVE_CALLER_CONTACT"])
        XCTAssertNil(restored["ATTENTIVE_CUSTOMER_ID"])
        XCTAssertNil(restored["SERVICE_TOKEN"])
    }

    func testLocalLaunchOverrideClearsSavedPermission() {
        let suite = "attentive.local-launch-test.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let approved = ["ATTENTIVE_CALL_ENDPOINT": "http://192.168.1.5:3004/api/public-agent/rvc-session",
                        "ATTENTIVE_LOCAL_DEVICE": "1"]
        for override in [
            ["ATTENTIVE_LOCAL_DEVICE": "0"],
            ["ATTENTIVE_CALL_ENDPOINT": "https://example.com/api/call"],
            ["ATTENTIVE_CALL_ENDPOINT": "http://192.168.1.5:3004/api/call"],
            ["ATTENTIVE_CALL_ENDPOINT": "http://8.8.8.8:3004", "ATTENTIVE_LOCAL_DEVICE": "1"],
            ["ATTENTIVE_CALL_ENDPOINT": "http://192.168.1.5:3005", "ATTENTIVE_LOCAL_DEVICE": "1"],
        ] {
            _ = SampleLocalLaunchSettings.environment(approved, defaults: defaults)
            XCTAssertEqual(SampleLocalLaunchSettings.environment(override, defaults: defaults), override)
            XCTAssertTrue(SampleLocalLaunchSettings.environment([:], defaults: defaults).isEmpty)
        }
    }

    func testLiveLocalCallAfterRelaunch() throws {
        let env = ProcessInfo.processInfo.environment
        guard env["ATTENTIVE_LIVE_UI_TEST"] == "1", env["ATTENTIVE_LOCAL_DEVICE"] == "1",
              let endpoint = env["ATTENTIVE_CALL_ENDPOINT"] else {
            throw XCTSkip("Opt in with the approved local-device live-test configuration.")
        }
        let app = XCUIApplication()
        app.launchArguments = ["--chat-only"]
        for key in ["ATTENTIVE_CALL_ENDPOINT", "ATTENTIVE_BUSINESS_SLUG", "ATTENTIVE_AGENT_ID", "ATTENTIVE_LOCAL_DEVICE"] {
            app.launchEnvironment[key] = env[key]
        }
        app.launch()
        XCTAssertTrue(app.buttons["startCall"].waitForExistence(timeout: 10))
        app.terminate()
        app.launchEnvironment = ["ATTENTIVE_CALLER_CONTACT": "mavino@odion.ai"]
        app.launch()
        XCTAssertTrue(app.buttons["startCall"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.alerts["Invalid configuration"].exists)
        app.buttons["bankMenu"].tap()
        app.buttons["callerDetails"].tap()
        XCTAssertEqual(app.textFields["Call endpoint"].value as? String, endpoint)
        app.buttons["Close"].tap()
        XCTAssertTrue(app.staticTexts["Voice enrolled for this email."].waitForExistence(timeout: 10))
        app.buttons["startCall"].tap()
        addTeardownBlock {
            if app.buttons["endCall"].exists { app.buttons["endCall"].tap() }
        }
        XCTAssertTrue(app.buttons["transcriptToggle"].waitForExistence(timeout: 10))
        app.buttons["transcriptToggle"].tap()
        XCTAssertTrue(app.images["audioReceived"].waitForExistence(timeout: 40))
        XCTAssertTrue(app.staticTexts.matching(identifier: "agentTranscript").firstMatch.waitForExistence(timeout: 10))
        attachScreen("Local configuration restored after normal launch")
        app.buttons["endCall"].tap()
    }

    func testLocalDeviceModeRequiresExplicitPrivateEndpoint() {
        let privateURL = URL(string: "http://192.168.1.5:3000/api/public-agent/connection-details")!
        XCTAssertFalse(LocalDeviceConnectionPolicy(endpoint: privateURL, environment: [:]).enabled)
        for address in ["http://example.com:3000", "http://8.8.8.8:3000", "http://172.32.0.1:3000",
                        "http://example.com:3004", "http://8.8.8.8:3004", "http://127.0.0.1:3004",
                        "http://192.168.1.5:3003", "http://192.168.1.5:3005",
                        "http://127.0.0.1:3000", "http://192.168.1.5:80", "https://192.168.1.5:3000",
                        "http://user:password@192.168.1.5:3000"] {
            XCTAssertFalse(LocalDeviceConnectionPolicy(endpoint: URL(string: address)!,
                environment: ["ATTENTIVE_LOCAL_DEVICE": "1"]).enabled)
        }
        #if DEBUG
        for address in ["192.168.1.5", "10.1.2.3", "172.16.0.1", "172.31.255.255"] {
            for port in [3000, 3004] {
                let url = URL(string: "http://\(address):\(port)")!
                XCTAssertFalse(LocalDeviceConnectionPolicy(endpoint: url, environment: [:]).enabled)
                XCTAssertTrue(LocalDeviceConnectionPolicy(endpoint: url,
                    environment: ["ATTENTIVE_LOCAL_DEVICE": "1"]).enabled)
            }
        }
        #else
        XCTAssertFalse(LocalDeviceConnectionPolicy(endpoint: privateURL,
            environment: ["ATTENTIVE_LOCAL_DEVICE": "1"]).enabled)
        #endif
    }

    func testLocalDeviceModeOnlyRewritesLoopbackSignaling() {
        let policy = LocalDeviceConnectionPolicy(endpoint: URL(string: "http://192.168.1.5:3000")!,
            environment: ["ATTENTIVE_LOCAL_DEVICE": "1"])
        let original = URL(string: "ws://127.0.0.1:7880/rtc?test=1")!
        #if DEBUG
        XCTAssertEqual(policy.signalingURL(original).absoluteString, "ws://192.168.1.5:7880/rtc?test=1")
        #else
        XCTAssertEqual(policy.signalingURL(original), original)
        #endif
        for address in ["wss://service.example.com/rtc", "ws://10.1.2.3:7880", "ws://127.0.0.1:9999"] {
            let url = URL(string: address)!
            XCTAssertEqual(policy.signalingURL(url), url)
        }
        XCTAssertEqual(LocalDeviceConnectionPolicy(endpoint: URL(string: "http://192.168.1.5:3000")!,
            environment: [:]).signalingURL(original), original)
    }

    func testComparisonLocalDeviceModePreservesSignalingCredentialsURL() {
        let policy = LocalDeviceConnectionPolicy(endpoint: URL(string: "http://192.168.1.5:3004/api/public-agent/rvc-session")!,
            environment: ["ATTENTIVE_LOCAL_DEVICE": "1"])
        let original = URL(string: "ws://127.0.0.1:7880/rtc?test=1")!
        #if DEBUG
        XCTAssertEqual(policy.signalingURL(original).absoluteString, "ws://192.168.1.5:7880/rtc?test=1")
        #else
        XCTAssertFalse(policy.enabled)
        XCTAssertEqual(policy.signalingURL(original), original)
        #endif
        let remote = URL(string: "wss://service.example.com/rtc")!
        XCTAssertEqual(policy.signalingURL(remote), remote)
    }

    func testGenericCallerHasNoBankBrandingOrRequiredEnrollmentUI() {
        let app = XCUIApplication()
        app.launchArguments = ["--generic-ui"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Talk to your agent"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["startCall"].isEnabled)
        XCTAssertFalse(app.buttons["recordVoice"].exists)
        app.buttons["bankMenu"].tap()
        XCTAssertTrue(app.staticTexts["My account"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["No activity yet"].exists)
        XCTAssertFalse(app.textFields["Wema customer ID"].exists)
        XCTAssertFalse(app.staticTexts["My Wema"].exists)
        attachScreen("Generic optional caller UI")
    }

    func testCallerDetailsAndActivity() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.buttons["startCall"].waitForExistence(timeout: 10))
        attachScreen("Public caller start screen")
        app.buttons["bankMenu"].tap()
        XCTAssertTrue(app.textFields["Wema customer ID"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.textFields["Phone number"].exists)
        XCTAssertTrue(app.staticTexts["No bank activity yet"].exists)
        app.buttons["LLM generated"].tap()
        XCTAssertTrue(app.buttons["LLM generated"].isSelected)
        app.buttons["Tool specific"].tap()
        attachScreen("My Wema floating panel")
        app.buttons["callerDetails"].tap()
        XCTAssertTrue(app.textFields["Customer ID"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.textFields["Phone number"].exists)
        app.buttons["saveSettings"].tap()
        XCTAssertTrue(app.buttons["startCall"].isHittable)
        XCUIDevice.shared.orientation = .landscapeLeft
        XCTAssertTrue(app.buttons["startCall"].isHittable)
        app.buttons["bankMenu"].tap()
        XCTAssertTrue(app.textFields["Wema customer ID"].waitForExistence(timeout: 5))
        attachScreen("Landscape bank panel")
        app.buttons["bankMenu"].tap()
        XCTAssertTrue(app.buttons["startCall"].isHittable)
        XCUIDevice.shared.orientation = .portrait
    }

    func testLiveChatCall() throws {
        guard ProcessInfo.processInfo.environment["ATTENTIVE_LIVE_UI_TEST"] == "1" else {
            throw XCTSkip("Opt in with TEST_RUNNER_ATTENTIVE_LIVE_UI_TEST=1; this test starts a real call.")
        }
        let app = XCUIApplication()
        app.launchArguments = ["--chat-only"]
        app.launchEnvironment["ATTENTIVE_CALLER_CONTACT"] = "attentive-ios-ui-test@odion.ai"
        for key in ["ATTENTIVE_CALL_ENDPOINT", "ATTENTIVE_BUSINESS_SLUG", "ATTENTIVE_AGENT_ID", "ATTENTIVE_LOCAL_DEVICE"] {
            if let value = ProcessInfo.processInfo.environment[key] {
                app.launchEnvironment[key] = value
            }
        }
        app.launch()
        XCTAssertTrue(app.buttons["startCall"].waitForExistence(timeout: 10))
        app.buttons["startCall"].tap()
        addTeardownBlock {
            if app.buttons["endCall"].exists { app.buttons["endCall"].tap() }
        }
        XCTAssertTrue(app.buttons["transcriptToggle"].waitForExistence(timeout: 10))
        attachScreen("Public caller call stage")
        if ProcessInfo.processInfo.environment["ATTENTIVE_AVATAR_CAPTURE"] == "1" {
            expectation(for: NSPredicate(format: "label == 'Speaking'"),
                        evaluatedWith: app.staticTexts["callStatus"])
            waitForExpectations(timeout: 45)
            for index in 0..<12 { attachScreen("Speech avatar \(index)") }
        }
        app.buttons["transcriptToggle"].tap()
        let messages = app.staticTexts.matching(identifier: "agentTranscript")
        XCTAssertTrue(messages.firstMatch.waitForExistence(timeout: 45))
        let listening = NSPredicate(format: "label == 'Listening'")
        expectation(for: listening, evaluatedWith: app.staticTexts["transcriptStatus"])
        waitForExpectations(timeout: 45)
        XCTAssertTrue(app.images["audioReceived"].waitForExistence(timeout: 10))
        let before = messages.count
        let input = app.textFields["chatInput"].exists ? app.textFields["chatInput"] : app.textViews["chatInput"]
        input.tap()
        input.typeText("Hello. Tell me in one short sentence what you can help with. Do not use any banking tools.")
        attachScreen("Chat composer")
        app.buttons["sendMessage"].tap()
        let response = NSPredicate { _, _ in messages.count > before }
        expectation(for: response, evaluatedWith: app)
        waitForExpectations(timeout: 40)
        expectation(for: listening, evaluatedWith: app.staticTexts["transcriptStatus"])
        waitForExpectations(timeout: 15)
        attachScreen("Native agent response")
        app.buttons["bankMenu"].tap()
        XCTAssertTrue(app.buttons["endCall"].isHittable)
        attachScreen("Connected public caller")
        app.buttons["bankMenu"].tap()
        XCTAssertFalse(app.textFields["Wema customer ID"].isEnabled)
        XCTAssertFalse(app.buttons["LLM generated"].isEnabled)
        attachScreen("In-call bank panel")
    }

    func testLiveBankLookupShowsBackendActivityWithoutBypassingVoiceAuth() throws {
        guard ProcessInfo.processInfo.environment["ATTENTIVE_BANK_UI_TEST"] == "1" else {
            throw XCTSkip("Opt in with TEST_RUNNER_ATTENTIVE_BANK_UI_TEST=1; this starts a real unauthenticated lookup.")
        }
        let app = XCUIApplication()
        app.launchArguments = ["--chat-only"]
        app.launchEnvironment["ATTENTIVE_CALLER_CONTACT"] = "attentive-bank-ui-test@odion.ai"
        app.launchEnvironment["ATTENTIVE_CUSTOMER_ID"] = "R008448055"
        app.launchEnvironment["ATTENTIVE_PHONE"] = "08161540638"
        app.launch()
        XCTAssertTrue(app.buttons["startCall"].waitForExistence(timeout: 10))
        app.buttons["startCall"].tap()
        addTeardownBlock {
            if app.buttons["endCall"].exists { app.buttons["endCall"].tap() }
        }
        XCTAssertTrue(app.buttons["transcriptToggle"].waitForExistence(timeout: 10))
        app.buttons["transcriptToggle"].tap()
        let greeting = app.staticTexts.matching(identifier: "agentTranscript").firstMatch
        XCTAssertTrue(greeting.waitForExistence(timeout: 45))
        expectation(for: NSPredicate(format: "label == 'Listening'"),
                    evaluatedWith: app.staticTexts["transcriptStatus"])
        waitForExpectations(timeout: 45)
        XCTAssertFalse(greeting.label.localizedCaseInsensitiveContains("fidelity"))
        let input = app.textFields["chatInput"].exists ? app.textFields["chatInput"] : app.textViews["chatInput"]
        input.tap()
        input.typeText("Please check my account balance.")
        app.buttons["sendMessage"].tap()
        // The X closes the transcript first; the hamburger then opens the bank panel.
        app.buttons["bankMenu"].tap()
        app.buttons["bankMenu"].tap()
        XCTAssertTrue(app.textFields["Wema customer ID"].waitForExistence(timeout: 5))
        let activity = app.staticTexts["Check balance"].firstMatch
        XCTAssertTrue(activity.waitForExistence(timeout: 45), "The model must invoke the real balance tool.")
        XCTAssertTrue(app.staticTexts["Failed"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.staticTexts["No bank activity yet"].exists)
        activity.tap()
        XCTAssertTrue(app.staticTexts["RESULT"].waitForExistence(timeout: 5))
        let result = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "voice_not_recognized")).firstMatch
        XCTAssertTrue(result.waitForExistence(timeout: 5))
        attachScreen("Real Wema balance activity blocked by voice authentication")
    }

    func testEnrollmentEmailValidationAndCancellation() {
        let app = XCUIApplication()
        app.launchEnvironment["ATTENTIVE_CALL_ENDPOINT"] = "http://127.0.0.1:3000/api/public-agent/connection-details"
        app.launchEnvironment["ATTENTIVE_CALLER_CONTACT"] = "08123456789"
        app.launch()
        let record = app.buttons["recordVoice"]
        XCTAssertTrue(record.waitForExistence(timeout: 10))
        record.tap()
        XCTAssertTrue(app.staticTexts["Enter a valid email for voice enrollment."].waitForExistence(timeout: 5))
        let contact = app.textFields["callerContact"]
        contact.tap()
        contact.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 11)
                         + "attentive-enrollment-cancel@example.com\n")
        // End editing without starting a call or touching the enrollment request.
        app.staticTexts["Talk to Wema Bank"].tap()
        expectation(for: NSPredicate(format: "enabled == true"), evaluatedWith: record)
        waitForExpectations(timeout: 20)
        record.tap()
        let cancel = app.buttons["cancelEnrollment"]
        XCTAssertTrue(cancel.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["startCall"].isEnabled)
        XCTAssertFalse(contact.isEnabled)
        cancel.tap()
        XCTAssertTrue(app.buttons["startCall"].isEnabled)
        XCTAssertTrue(contact.isEnabled)
        XCTAssertTrue(record.exists)
        attachScreen("Pre-call voice enrollment after cancellation")
    }

    private func attachScreen(_ name: String) {
        // Let the 180 ms panel transition and keyboard animation finish before capture.
        Thread.sleep(forTimeInterval: 0.6)
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
