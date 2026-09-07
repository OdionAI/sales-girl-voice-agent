import XCTest

final class SampleUITests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    func testLocalDeviceModeRequiresExplicitPrivateEndpoint() {
        let privateURL = URL(string: "http://192.168.1.5:3000/api/public-agent/connection-details")!
        XCTAssertFalse(LocalDeviceConnectionPolicy(endpoint: privateURL, environment: [:]).enabled)
        for address in ["http://example.com:3000", "http://8.8.8.8:3000", "http://172.32.0.1:3000",
                        "http://127.0.0.1:3000", "http://192.168.1.5:80", "https://192.168.1.5:3000",
                        "http://user:password@192.168.1.5:3000"] {
            XCTAssertFalse(LocalDeviceConnectionPolicy(endpoint: URL(string: address)!,
                environment: ["ATTENTIVE_LOCAL_DEVICE": "1"]).enabled)
        }
        #if DEBUG
        for address in ["192.168.1.5", "10.1.2.3", "172.16.0.1", "172.31.255.255"] {
            XCTAssertTrue(LocalDeviceConnectionPolicy(endpoint: URL(string: "http://\(address):3000")!,
                environment: ["ATTENTIVE_LOCAL_DEVICE": "1"]).enabled)
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
