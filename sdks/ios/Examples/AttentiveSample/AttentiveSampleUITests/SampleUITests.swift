import XCTest

final class SampleUITests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

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
