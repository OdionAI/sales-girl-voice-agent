import XCTest
@testable import AttentiveVoice

final class CallTests: XCTestCase {
    func testRequestCarriesCallerProfileWithoutModelOverrides() throws {
        let request = CallRequest(businessSlug: "bank", agentPublicId: "agt_test", endUserContact: "caller@example.com",
            profile: .init(customerId: "TEST_CUSTOMER", accountNumber: "0000000000", phoneNumber: "08000000000"),
            toolWaitSpeechMode: .llmGenerated)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(object["toolWaitSpeechMode"] as? String, "llm_generated")
        XCTAssertEqual((object["wemaContext"] as? [String: String])?["phoneNumber"], "08000000000")
        XCTAssertNil(object["runtime_overrides"])
    }

    func testSecureConnectionsByDefault() throws {
        XCTAssertThrowsError(try HTTPCallCredentialProvider(endpoint: URL(string: "http://localhost:3000")!))
        XCTAssertNoThrow(try HTTPCallCredentialProvider(endpoint: URL(string: "http://localhost:3000")!, allowsInsecureDevelopmentConnections: true))
        XCTAssertThrowsError(try HTTPCallCredentialProvider(endpoint: URL(string: "https://user:password@example.com")!))
        XCTAssertNoThrow(try HTTPCallCredentialProvider(endpoint: URL(string: "https://example.com/api/call")!))
    }

    func testCredentialsDescriptionIsRedacted() {
        let credentials = StubProvider.credentials
        XCTAssertFalse(String(describing: credentials).contains(credentials.participantToken))
        XCTAssertFalse(String(reflecting: credentials).contains(credentials.participantToken))
    }

    func testBackendEventsAndUnknownPayloads() throws {
        let data = Data(#"{"call_id":"c1","tool_name":"wema_get_balance","event":"completed","status":"success","result":{"amount":12,"ok":true}}"#.utf8)
        guard case .toolActivity(let activity) = BackendEventDecoder.decode(data: data, topic: "odion.tool.activity") else { return XCTFail() }
        XCTAssertEqual(activity.id, "c1")
        XCTAssertEqual(activity.payload["result"], .object(["amount": .number(12), "ok": .bool(true)]))
        XCTAssertNil(BackendEventDecoder.decode(data: data, topic: "unknown"))
        XCTAssertNil(BackendEventDecoder.decode(data: Data("bad".utf8), topic: "odion.auth.status"))
        XCTAssertEqual(BackendEventDecoder.decode(data: Data(#"{"status":"failed","authenticated":true}"#.utf8), topic: "odion.auth.status"),
                       .authentication(.init(scope: .session, status: .failed)))
    }

    @MainActor
    func testCallLifecycleEventsAndMic() async throws {
        let transport = MockTransport()
        let call = makeCall(transport)
        try await call.start(request)
        XCTAssertEqual(call.state, .connected)
        XCTAssertTrue(call.microphoneEnabled)
        transport.onEvent?(.agentStateChanged(.listening))
        try await call.sendText("Hello")
        XCTAssertEqual(transport.messages, ["Hello"])
        XCTAssertEqual(call.transcripts.last?.text, "Hello")
        try await call.setMicrophone(enabled: false)
        XCTAssertFalse(call.microphoneEnabled)
        transport.onEvent?(.authentication(.init(scope: .session, status: .verified)))
        transport.onEvent?(.authentication(.init(scope: .action, status: .failed)))
        XCTAssertEqual(call.sessionAuthentication, .verified)
        XCTAssertEqual(call.actionAuthentication, .failed)
        await call.end()
        XCTAssertEqual(call.state, .ended)
        XCTAssertTrue(transport.disconnected)
        XCTAssertEqual(call.sessionAuthentication, .pending)
    }

    @MainActor
    func testTranscriptRevisionsDoNotDuplicateAndFinalDoesNotRegress() async throws {
        let transport = MockTransport()
        let call = makeCall(transport)
        try await call.start(request, microphoneEnabled: false)
        for (text, final) in [("Hel", false), ("Hello", true), ("He", false)] {
            transport.onEvent?(.transcript(.init(id: "s1", speaker: .agent, text: text, isFinal: final)))
        }
        XCTAssertEqual(call.transcripts.count, 1)
        XCTAssertEqual(call.transcripts[0].text, "Hello")
        await call.end()
    }

    @MainActor
    func testEndDuringTokenFetchCannotReconnect() async throws {
        let transport = MockTransport()
        let call = makeCall(transport, provider: StubProvider(delay: .seconds(20)))
        let pending = Task { try await call.start(request) }
        await Task.yield()
        await call.end()
        _ = try? await pending.value
        XCTAssertEqual(transport.connections, 0)
        XCTAssertEqual(call.state, .ended)
    }

    @MainActor
    func testDuplicateStartRejectedAndStaleEventsIgnored() async throws {
        let transport = MockTransport()
        let call = makeCall(transport)
        try await call.start(request)
        do { try await call.start(request); XCTFail() } catch { XCTAssertEqual(error as? CallError, .alreadyActive) }
        let stale = transport.onEvent
        await call.end()
        stale?(.agentStateChanged(.speaking))
        XCTAssertEqual(call.state, .ended)
        XCTAssertEqual(call.agentState, .disconnected)
    }

    @MainActor
    func testMicDeniedDoesNotCreateSession() async {
        let transport = MockTransport()
        let call = AttentiveCall(provider: StubProvider(), makeTransport: { transport }, microphonePermission: { false })
        do { try await call.start(request); XCTFail() } catch { XCTAssertEqual(error as? CallError, .microphoneDenied) }
        XCTAssertEqual(transport.connections, 0)
        XCTAssertEqual(call.state, .failed)
    }

    @MainActor
    func testMissingAgentTimesOut() async throws {
        let transport = MockTransport()
        let call = AttentiveCall(provider: StubProvider(), makeTransport: { transport },
            microphonePermission: { true }, agentTimeout: .milliseconds(10))
        try await call.start(request, microphoneEnabled: false)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(call.state, .failed)
        XCTAssertEqual(call.lastError, .agentUnavailable)
        XCTAssertTrue(transport.disconnected)
    }

    private var request: CallRequest { .init(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "caller@example.com") }

    @MainActor
    private func makeCall(_ transport: MockTransport, provider: StubProvider = .init()) -> AttentiveCall {
        AttentiveCall(provider: provider, makeTransport: { transport }, microphonePermission: { true })
    }
}

private struct StubProvider: CallCredentialProvider {
    static let credentials = CallCredentials(serverUrl: URL(string: "wss://example.com")!, roomName: "test", participantToken: "private-token")
    var delay: Duration = .zero
    func fetch(for request: CallRequest) async throws -> CallCredentials {
        try await Task.sleep(for: delay)
        return Self.credentials
    }
}

@MainActor
private final class MockTransport: CallTransport {
    var onEvent: ((CallEvent) -> Void)?
    var disconnected = false
    var connections = 0
    var messages: [String] = []
    func connect(_ credentials: CallCredentials, microphoneEnabled: Bool) async throws { connections += 1 }
    func disconnect() async { disconnected = true }
    func setMicrophone(enabled: Bool) async throws {}
    func sendText(_ text: String) async throws -> String { messages.append(text); return UUID().uuidString }
}
