import Combine
import XCTest
@testable import AttentiveVoice
@testable import AttentiveVoiceUI

final class CallerControlsTests: XCTestCase {
    #if os(iOS)
    @available(iOS 17, *)
    func testDefaultCallerHidesAccountAndSettingsMenus() {
        let configuration = CallerUIConfiguration()
        XCTAssertFalse(configuration.showsAccountMenu)
        XCTAssertFalse(configuration.showsCallOptions)
    }
    #endif

    @MainActor
    func testAutomaticPresentationStartsOnceAndDoesNotRedialAfterEnd() async {
        let transport = UITransport()
        let call = makeCall(transport)
        let controls = CallerControls(call: call, enrollment: nil)
        let start = { try await call.start(self.request, microphoneEnabled: false) }
        await controls.startAutomatically(start)
        await controls.startAutomatically(start)
        XCTAssertEqual(transport.connections, 1)
        await controls.end()
        await controls.disappear(endsCall: true)
        controls.appear()
        await controls.startAutomatically(start)
        XCTAssertEqual(transport.connections, 1)
        let nextPresentation = CallerControls(call: call, enrollment: nil)
        await nextPresentation.startAutomatically(start)
        XCTAssertEqual(transport.connections, 2)
        await nextPresentation.end()
    }

    @MainActor
    func testEndBeforeAutomaticTaskRunsCannotStartACall() async {
        let controls = CallerControls(call: makeCall(UITransport()), enrollment: nil)
        await controls.end()
        await controls.startAutomatically { XCTFail("A cancelled presentation cannot start") }
    }

    @MainActor
    func testAutomaticFailureDoesNotRetryAndEndCancelsHostTokenFetch() async {
        let transport = UITransport()
        let call = makeCall(transport)
        let controls = CallerControls(call: call, enrollment: nil)
        var attempts = 0
        let failure: () async throws -> Void = { attempts += 1; throw CallError.callerRequired }
        await controls.startAutomatically(failure)
        await controls.startAutomatically(failure)
        XCTAssertEqual(attempts, 1)
        XCTAssertNotNil(controls.errorMessage)
        let next = CallerControls(call: call, enrollment: nil)
        let startup = Task {
            await next.startAutomatically {
                attempts += 1
                try await Task.sleep(for: .seconds(20))
                XCTFail("End must cancel token fetching before a room is created")
            }
        }
        while attempts != 2 { await Task.yield() }
        await next.end()
        await startup.value
        XCTAssertFalse(next.starting)
        XCTAssertEqual(call.state, .ended)
        XCTAssertEqual(transport.connections, 0)
        XCTAssertNil(next.errorMessage)
    }

    @MainActor
    func testBankActivityReplacesStartedRowWithBackendResult() async throws {
        let transport = UITransport()
        let call = makeCall(transport)
        let controls = CallerControls(call: call, enrollment: nil)
        await controls.start(request, microphoneEnabled: false)
        let started = Data(#"{"call_id":"balance-1","tool_name":"wema_get_balance","event":"started","arguments":{}}"#.utf8)
        transport.onEvent?(try XCTUnwrap(BackendEventDecoder.decode(data: started, topic: "odion.tool.activity")))
        XCTAssertEqual(call.toolActivity.count, 1)
        XCTAssertEqual(call.toolActivity.first?.event, "started")
        let completed = Data(#"{"call_id":"balance-1","tool_name":"wema_get_balance","event":"completed","status":"success","result":{"status":"success","data":{"currency":"NGN","balance":123}}}"#.utf8)
        transport.onEvent?(try XCTUnwrap(BackendEventDecoder.decode(data: completed, topic: "odion.tool.activity")))
        XCTAssertEqual(call.toolActivity.count, 1)
        XCTAssertEqual(call.toolActivity.first?.toolName, "wema_get_balance")
        XCTAssertEqual(call.toolActivity.first?.status, "success")
        XCTAssertNotNil(call.toolActivity.first?.payload["result"])
        // Activity observations must not grant authentication or trigger a second request.
        XCTAssertEqual(call.sessionAuthentication, .pending)
        XCTAssertEqual(call.actionAuthentication, .pending)
        XCTAssertTrue(transport.messages.isEmpty)
        await controls.end()
    }

    @MainActor
    func testUIObservesWithoutReplacingHostEventsOrAuthorizingTools() async throws {
        let transport = UITransport()
        let call = makeCall(transport)
        var events: [CallEvent] = []
        call.onEvent = { events.append($0) }
        let controls = CallerControls(call: call, enrollment: nil)
        XCTAssertEqual(transport.connections, 0)
        await controls.start(request, microphoneEnabled: true)
        transport.onEvent?(.authentication(.init(scope: .session, status: .verified)))
        transport.onEvent?(.authentication(.init(scope: .action, status: .failed)))
        let data = Data(#"{"call_id":"test-call","tool_name":"balance","event":"completed","status":"blocked","result":{"message":"Voice check required"}}"#.utf8)
        let event = try XCTUnwrap(BackendEventDecoder.decode(data: data, topic: "odion.tool.activity"))
        transport.onEvent?(event)
        XCTAssertTrue(events.contains(event))
        XCTAssertEqual(call.toolActivity.first?.status, "blocked")
        XCTAssertEqual(call.sessionAuthentication, .verified)
        XCTAssertEqual(call.actionAuthentication, .failed)
        XCTAssertTrue(call.microphoneEnabled)
        XCTAssertTrue(transport.messages.isEmpty)
        await controls.end()
    }

    @MainActor
    func testRequestAndMicrophoneSelectionReachCoreUnchanged() async {
        let provider = UIProvider()
        let transport = UITransport()
        let call = makeCall(transport, provider: provider)
        let controls = CallerControls(call: call, enrollment: nil)
        await controls.start(request, microphoneEnabled: false)
        let sent = await provider.request
        XCTAssertEqual(sent?.wemaContext, request.wemaContext)
        XCTAssertEqual(sent?.endUserContact, request.endUserContact)
        XCTAssertEqual(sent?.toolWaitSpeechMode, .llmGenerated)
        XCTAssertEqual(transport.microphoneSelections, [false])
        XCTAssertTrue(controls.active)
        await controls.end()
    }

    @MainActor
    func testChatAndMuteUseCoreAndErrorsRemainVisible() async {
        let transport = UITransport()
        let call = makeCall(transport)
        let controls = CallerControls(call: call, enrollment: nil)
        await controls.start(request, microphoneEnabled: false)
        transport.onEvent?(.agentStateChanged(.listening))
        let sent = await controls.send("Hello")
        XCTAssertTrue(sent)
        XCTAssertEqual(transport.messages, ["Hello"])
        XCTAssertFalse(controls.sending)
        await controls.toggleMicrophone()
        XCTAssertTrue(call.microphoneEnabled)
        await controls.end()
        let afterEnd = await controls.send("Hello")
        XCTAssertFalse(afterEnd)
        XCTAssertNotNil(controls.errorMessage)
    }

    @MainActor
    func testAudioDoesNotContinuouslyInvalidateCallerScreenAndResetsForNewCall() async {
        let transport = UITransport()
        let call = makeCall(transport)
        let controls = CallerControls(call: call, enrollment: nil)
        await controls.start(request, microphoneEnabled: false)
        var updates = 0
        let observation = controls.objectWillChange.sink { updates += 1 }
        defer { observation.cancel() }
        transport.onEvent?(.agentAudioEnergy(0.3))
        transport.onEvent?(.agentAudioEnergy(0.7))
        transport.onEvent?(.agentAudioEnergy(0))
        transport.onEvent?(.agentAudioEnergy(0.1))
        XCTAssertTrue(controls.receivedAudio)
        XCTAssertEqual(updates, 1)
        await controls.end()
        await controls.start(request, microphoneEnabled: false)
        XCTAssertFalse(controls.receivedAudio)
        await controls.end()
    }

    @MainActor
    func testEnrollmentCaptureBlocksCallAndCancelsOnDismissal() async {
        let transport = UITransport()
        let recorder = UIRecorder()
        let enrollment = VoiceEnrollment(provider: UIEnrollmentProvider(), recorder: recorder)
        let controls = CallerControls(call: makeCall(transport), enrollment: enrollment)
        let recording = Task { await enrollment.record(email: "ui-test@example.com") }
        while enrollment.stage != .recording { await Task.yield() }
        XCTAssertTrue(controls.enrollmentBusy)
        await controls.start(request, microphoneEnabled: true)
        XCTAssertEqual(transport.connections, 0)
        await controls.disappear(endsCall: true)
        await recording.value
        XCTAssertTrue(recorder.cancelled)
        XCTAssertFalse(controls.enrollmentBusy)
        await controls.start(request, microphoneEnabled: false)
        XCTAssertEqual(transport.connections, 0)
        controls.appear()
        await controls.start(request, microphoneEnabled: false)
        XCTAssertEqual(transport.connections, 1)
        await controls.end()
    }

    @MainActor
    func testDismissalCancelsCallCreationWithoutReconnecting() async {
        let transport = UITransport()
        let call = makeCall(transport, provider: UIProvider(delay: .seconds(20)))
        let controls = CallerControls(call: call, enrollment: nil)
        let starting = Task { await controls.start(request, microphoneEnabled: false) }
        while call.state != .connecting { await Task.yield() }
        await controls.disappear(endsCall: true)
        await starting.value
        XCTAssertEqual(call.state, .ended)
        XCTAssertEqual(transport.connections, 0)
    }

    @MainActor
    func testHostCanExplicitlyRetainCallWhenDismissingUI() async {
        let transport = UITransport()
        let call = makeCall(transport)
        let controls = CallerControls(call: call, enrollment: nil)
        await controls.start(request, microphoneEnabled: false)
        await controls.disappear(endsCall: false)
        XCTAssertEqual(call.state, .connected)
        XCTAssertEqual(transport.disconnects, 0)
        await call.end()
    }

    @MainActor
    func testCallerTokenLoadingCannotBeStartedTwiceAndCancelsOnDismissal() async {
        let transport = UITransport()
        let controls = CallerControls(call: makeCall(transport), enrollment: nil)
        var tokenRequests = 0
        let first = Task {
            await controls.start {
                tokenRequests += 1
                try await Task.sleep(for: .seconds(20))
                XCTFail("Dismissal must cancel the caller-token request")
            }
        }
        while tokenRequests == 0 { await Task.yield() }
        XCTAssertTrue(controls.starting)
        await controls.start { tokenRequests += 1 }
        XCTAssertEqual(tokenRequests, 1)
        await controls.disappear(endsCall: true)
        await first.value
        XCTAssertFalse(controls.starting)
        XCTAssertNil(controls.errorMessage)
        XCTAssertEqual(transport.connections, 0)
    }

    @MainActor
    func testCorePermissionFailureDoesNotCreateCallOrGetRetriedByUI() async {
        let transport = UITransport()
        let call = AttentiveCall(provider: UIProvider(), makeTransport: { transport }, microphonePermission: { false })
        let controls = CallerControls(call: call, enrollment: nil)
        await controls.start(request, microphoneEnabled: true)
        XCTAssertEqual(transport.connections, 0)
        XCTAssertEqual(call.state, .failed)
        XCTAssertEqual(controls.errorMessage, CallError.microphoneDenied.localizedDescription)
        XCTAssertFalse(controls.active)
    }

    private var request: CallRequest {
        .init(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "ui-test@example.com",
              profile: .init(customerId: "TEST_CUSTOMER", accountNumber: "0000000000", phoneNumber: "08000000000"),
              toolWaitSpeechMode: .llmGenerated)
    }

    @MainActor
    func testInsufficientCreditNotifiesHostAndUIWithoutConnectingOrRetrying() async {
        let error = CallError.insufficientCredits(balanceKobo: 0, requiredMinimumKobo: 167)
        let provider = UIProvider(error: error)
        let transport = UITransport()
        let call = makeCall(transport, provider: provider)
        var events: [CallEvent] = []
        call.onEvent = { events.append($0) }
        let controls = CallerControls(call: call, enrollment: nil)

        await controls.start(request, microphoneEnabled: false)

        XCTAssertEqual(call.lastError, error)
        XCTAssertEqual(call.state, .failed)
        XCTAssertEqual(controls.errorTitle, "Call credit needed")
        XCTAssertEqual(controls.errorMessage, error.localizedDescription)
        XCTAssertFalse(controls.active)
        XCTAssertEqual(transport.connections, 0)
        XCTAssertTrue(transport.microphoneSelections.isEmpty)
        XCTAssertTrue(transport.messages.isEmpty)
        XCTAssertEqual(call.sessionAuthentication, .pending)
        XCTAssertEqual(call.actionAuthentication, .pending)
        XCTAssertTrue(events.contains(.failure(error)))
        controls.errorMessage = nil
        await Task.yield()
        let fetches = await provider.fetches
        XCTAssertEqual(fetches, 1)
        XCTAssertNil(controls.errorMessage)
    }

    @MainActor
    private func makeCall(_ transport: UITransport, provider: UIProvider = .init()) -> AttentiveCall {
        AttentiveCall(provider: provider, makeTransport: { transport }, microphonePermission: { true })
    }
}

private actor UIProvider: CallCredentialProvider {
    private(set) var request: CallRequest?
    private(set) var fetches = 0
    let delay: Duration
    let error: CallError?
    init(delay: Duration = .zero, error: CallError? = nil) { self.delay = delay; self.error = error }
    func fetch(for request: CallRequest) async throws -> CallCredentials {
        self.request = request
        fetches += 1
        try await Task.sleep(for: delay)
        if let error { throw error }
        return .init(serverUrl: URL(string: "wss://example.invalid")!, roomName: "test", participantToken: "test-only")
    }
}

@MainActor
private final class UITransport: CallTransport {
    var onEvent: ((CallEvent) -> Void)?
    var connections = 0
    var disconnects = 0
    var microphoneSelections: [Bool] = []
    var messages: [String] = []
    func connect(_ credentials: CallCredentials, microphoneEnabled: Bool) async throws {
        connections += 1
        microphoneSelections.append(microphoneEnabled)
    }
    func disconnect() async { disconnects += 1 }
    func setMicrophone(enabled: Bool) async throws { microphoneSelections.append(enabled) }
    func sendText(_ text: String) async throws -> String { messages.append(text); return UUID().uuidString }
}

private struct UIEnrollmentProvider: VoiceEnrollmentProvider {
    func isEnrolled(email: String) async throws -> Bool { false }
    func enroll(email: String, wav: Data) async throws { XCTFail("Cancelled recordings must never upload") }
}

@MainActor
private final class UIRecorder: VoiceEnrollmentRecording {
    var cancelled = false
    func record(progress: @escaping @MainActor (Int) -> Void) async throws -> Data {
        try await Task.sleep(for: .seconds(20))
        return Data()
    }
    func cancel() { cancelled = true }
}
