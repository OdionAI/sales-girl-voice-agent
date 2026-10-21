import Foundation
import XCTest
@testable import AttentiveVoice

final class PasskeyValueTests: XCTestCase {
    #if os(iOS)
    @MainActor
    func testNativePresentationRequiresTheCallScreensWindow() async throws {
        let authorizer = NativePasskeyAuthorizer(anchor: { nil })
        let request = try PasskeyRequest(ceremonyPayload(),
            configuration: .init(relyingPartyIdentifiers: ["auth.example.com"]), now: Date())
        do { _ = try await authorizer.authorize(request); XCTFail("A missing scene must not open a prompt") }
        catch { XCTAssertEqual(error as? PasskeyFailure, .presentationUnavailable) }
        authorizer.cancel()
    }
    #endif

    func testConfigurationRejectsURLsAndInvalidLimitsAndRedactsDeviceIdentifier() throws {
        let configuration = AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["auth.example.com"], deviceIdentifier: "private-device-value")
        XCTAssertNoThrow(try configuration.validate())
        XCTAssertThrowsError(try AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["https://auth.example.com"]).validate())
        XCTAssertThrowsError(try AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["auth..example.com"]).validate())
        XCTAssertThrowsError(try AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["auth.example.com"], ceremonyTimeoutSeconds: .infinity).validate())
        XCTAssertThrowsError(try AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["auth.example.com"], verificationTimeoutSeconds: 0).validate())
        XCTAssertFalse(String(describing: configuration).contains("private-device-value"))
        XCTAssertFalse(String(reflecting: configuration).contains("private-device-value"))
        var dumped = ""; dump(configuration, to: &dumped)
        XCTAssertFalse(dumped.contains("private-device-value"))
    }

    func testBase64URLIsCanonicalAndPreservesBytes() throws {
        let bytes = Data([0, 1, 254, 255])
        XCTAssertEqual(try PasskeyEncoding.decode(PasskeyEncoding.encode(bytes)), bytes)
        for invalid in ["", "A", "AB", "AQ==", "a+b/", " abc", String(repeating: "a", count: 9000)] {
            XCTAssertThrowsError(try PasskeyEncoding.decode(invalid))
        }
    }

    func testDistinctRegistrationAndAssertionSchemas() throws {
        let assertion = PasskeyProof.assertion(id: Data([255]), clientData: Data([1]), authenticatorData: Data([2]), signature: Data([3]), userID: Data())
        let registration = PasskeyProof.registration(id: Data([255]), clientData: Data([1]), attestation: Data([4]))
        guard case .object(let a) = assertion.fields["response"], case .object(let r) = registration.fields["response"] else { return XCTFail() }
        XCTAssertEqual(assertion.fields["id"], .string("_w"))
        XCTAssertEqual(a["userHandle"], .null)
        XCTAssertNotNil(a["signature"]); XCTAssertNil(a["attestationObject"])
        XCTAssertNotNil(r["attestationObject"]); XCTAssertNil(r["signature"]); XCTAssertNil(r["userHandle"])
        XCTAssertFalse(String(reflecting: assertion).contains("signature"))
    }

    func testOptionMappingAndUnsupportedRequirements() throws {
        let now = Date()
        let config = AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["auth.example.com"])
        var payload = ceremonyPayload(expiry: now.addingTimeInterval(10))
        let request = try PasskeyRequest(payload, configuration: config, now: now)
        XCTAssertEqual(request.challenge, Data([1, 2, 3]))
        XCTAssertEqual(request.authenticationID, "Mixed-Case-ID")
        XCTAssertLessThanOrEqual(request.promptTimeout, 10)
        payload["options"] = .object(["challenge": .string("AQID"), "rpId": .string("evil.example.com")])
        XCTAssertThrowsError(try PasskeyRequest(payload, configuration: config, now: now))
        for option in ["extensions": JSONValue.object(["prf": .object([:])]), "userVerification": .string("invalid"), "unknownRequirement": .bool(true)] {
            var options: [String: JSONValue] = ["challenge": .string("AQID"), "rpId": .string("auth.example.com")]
            options[option.key] = option.value; payload["options"] = .object(options)
            XCTAssertThrowsError(try PasskeyRequest(payload, configuration: config, now: now))
        }
        payload = ceremonyPayload(expiry: now.addingTimeInterval(-1))
        XCTAssertThrowsError(try PasskeyRequest(payload, configuration: config, now: now))
    }

    func testRegistrationMapsUserIDAndExclusionsAndRequiresSupportedAlgorithm() throws {
        var payload = ceremonyPayload()
        payload["mode"] = .string("register")
        var options: [String: JSONValue] = ["challenge": .string("AQID"),
            "rp": .object(["id": .string("auth.example.com"), "name": .string("Example")]),
            "user": .object(["id": .string("BAU"), "name": .string("customer"), "displayName": .string("Customer")]),
            "pubKeyCredParams": .array([.object(["type": .string("public-key"), "alg": .number(-7)])]),
            "excludeCredentials": .array([.object(["type": .string("public-key"), "id": .string("Bg")])])]
        payload["options"] = .object(options)
        let config = AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["auth.example.com"])
        let request = try PasskeyRequest(payload, configuration: config, now: Date())
        XCTAssertEqual(request.mode, .register); XCTAssertEqual(request.userID, Data([4, 5]))
        XCTAssertEqual(request.credentialIDs, [Data([6])])
        options["pubKeyCredParams"] = .array([.object(["type": .string("public-key"), "alg": .number(-257)])])
        payload["options"] = .object(options)
        XCTAssertThrowsError(try PasskeyRequest(payload, configuration: config, now: Date()))
    }
}

@MainActor
final class PasskeyCoordinatorTests: XCTestCase {
    func testNativeSuccessStaysPendingUntilMatchingServerAcceptance() async throws {
        let fixture = PasskeyFixture()
        try await fixture.ready()
        fixture.message(ceremonyPayload())
        try await waitUntil { fixture.messages.contains { $0["type"] == .string("fido2_proof") } }
        XCTAssertEqual(fixture.statuses.last, .pending)
        XCTAssertEqual(fixture.messages.last?["authenticationId"], .string("Mixed-Case-ID"))
        fixture.message(["type": .string("fido2_result"), "authenticationId": .string("wrong"), "accepted": .bool(true)])
        XCTAssertEqual(fixture.statuses.last, .pending)
        fixture.message(["type": .string("fido2_result"), "authenticationId": .string("Mixed-Case-ID"), "accepted": .string("true")])
        XCTAssertEqual(fixture.statuses.last, .pending)
        fixture.result(accepted: true)
        XCTAssertEqual(fixture.statuses.last, .verified)
        fixture.result(accepted: false)
        XCTAssertEqual(fixture.statuses.last, .verified)
        fixture.coordinator.suspend()
    }

    func testBankRejectionNeverShowsVerified() async throws {
        let fixture = PasskeyFixture(); try await fixture.ready()
        fixture.message(ceremonyPayload())
        try await waitUntil { fixture.messages.count == 2 }
        fixture.result(accepted: false)
        XCTAssertEqual(fixture.statuses.last, .failed)
        XCTAssertFalse(fixture.statuses.contains(.verified))
        fixture.coordinator.suspend()
    }

    func testNoPromptBeforeConnectionOrWithWrongVersionOrMissingCorrelation() async throws {
        let fixture = PasskeyFixture()
        fixture.message(ceremonyPayload())
        XCTAssertEqual(fixture.authorizer.requests, 0)
        try await fixture.ready()
        var wrong = ceremonyPayload(); wrong["version"] = .number(2); fixture.message(wrong)
        wrong = ceremonyPayload(); wrong["correlationId"] = nil
        fixture.coordinator.receive(.message(try JSONEncoder().encode(wrong)))
        XCTAssertEqual(fixture.authorizer.requests, 0)
        fixture.coordinator.suspend()
    }

    func testServerCannotVerifyBeforeNativeProof() async throws {
        let fixture = PasskeyFixture(); fixture.authorizer.hold = true
        try await fixture.ready(); fixture.message(ceremonyPayload())
        try await waitUntil { fixture.authorizer.requests == 1 }
        fixture.result(accepted: true)
        XCTAssertEqual(fixture.statuses.last, .pending)
        fixture.coordinator.suspend()
    }

    func testCancellationKeepsCallUsableAndDoesNotAuthorizeOrRetry() async throws {
        let fixture = PasskeyFixture(); fixture.authorizer.failure = .cancelled
        try await fixture.ready(); fixture.message(ceremonyPayload())
        try await waitUntil { fixture.messages.count == 2 }
        XCTAssertEqual(fixture.messages.last?["type"], .string("fido2_cancelled"))
        XCTAssertEqual(fixture.statuses.last, .pending)
        XCTAssertEqual(fixture.authorizer.requests, 1)
        fixture.result(accepted: true)
        XCTAssertEqual(fixture.statuses.last, .pending)
        fixture.coordinator.suspend()
    }

    func testDuplicateBusyAndReplayDoNotOpenAnotherPrompt() async throws {
        let fixture = PasskeyFixture(); fixture.authorizer.hold = true
        try await fixture.ready(); fixture.message(ceremonyPayload())
        try await waitUntil { fixture.authorizer.requests == 1 }
        fixture.message(ceremonyPayload())
        var other = ceremonyPayload(); other["authenticationId"] = .string("other"); other["correlationId"] = .string("other-correlation")
        fixture.message(other)
        try await waitUntil { fixture.messages.count == 2 }
        XCTAssertEqual(fixture.messages.last?["reason"], .string("busy"))
        fixture.authorizer.complete()
        try await waitUntil { fixture.messages.count == 3 }
        fixture.result(accepted: true)
        var replay = ceremonyPayload(); replay["authenticationId"] = .string("mixed-case-id")
        fixture.message(replay)
        try await waitUntil { fixture.messages.count == 4 }
        XCTAssertEqual(fixture.messages.last?["reason"], .string("replayed"))
        XCTAssertEqual(fixture.authorizer.requests, 1)
        fixture.coordinator.suspend()
    }

    func testExpiryCancelsPromptAndRejectsLateSuccess() async throws {
        let fixture = PasskeyFixture(); fixture.authorizer.hold = true; fixture.authorizer.ignoreCancellation = true
        try await fixture.ready()
        fixture.message(ceremonyPayload(expiry: Date().addingTimeInterval(0.05)))
        try await waitUntil { fixture.messages.count == 2 }
        XCTAssertEqual(fixture.messages.last?["reason"], .string("expired"))
        fixture.authorizer.complete()
        for _ in 0..<20 { await Task.yield() }
        XCTAssertFalse(fixture.messages.contains { $0["type"] == .string("fido2_proof") })
        fixture.result(accepted: true)
        XCTAssertEqual(fixture.statuses.last, .failed)
        fixture.coordinator.suspend()
    }

    func testReconnectNeverReplaysProofAndOldResultCannotAuthorize() async throws {
        let fixture = PasskeyFixture(); try await fixture.ready()
        fixture.message(ceremonyPayload()); try await waitUntil { fixture.messages.count == 2 }
        let previousContext = fixture.context
        fixture.coordinator.receive(.unavailable)
        try await fixture.ready()
        fixture.message(["type": .string("fido2_result"), "correlationId": .string(previousContext),
                         "authenticationId": .string("Mixed-Case-ID"), "accepted": .bool(true)])
        XCTAssertEqual(fixture.statuses.last, .pending)
        XCTAssertEqual(fixture.messages.filter { $0["type"] == .string("fido2_proof") }.count, 1)
        fixture.message(ceremonyPayload())
        try await waitUntil { fixture.messages.count == 4 }
        XCTAssertEqual(fixture.messages.last?["reason"], .string("replayed"))
        fixture.coordinator.suspend()
    }

    func testPublishFailureDoesNotRetryOrExposeCredentialInEvents() async throws {
        let fixture = PasskeyFixture(); try await fixture.ready(); fixture.failSend = true
        fixture.message(ceremonyPayload())
        try await waitUntil { fixture.statuses.last == .failed }
        XCTAssertFalse(fixture.statuses.contains(.verified))
        XCTAssertEqual(fixture.authorizer.requests, 1)
        fixture.coordinator.suspend()
    }

    func testProofWaitExpiresAndServerAbortRejectsLateNativeCompletion() async throws {
        let fixture = PasskeyFixture(); try await fixture.ready()
        fixture.message(ceremonyPayload(expiry: Date().addingTimeInterval(0.05)))
        try await waitUntil { fixture.messages.count == 3 }
        XCTAssertEqual(fixture.messages[1]["type"], .string("fido2_proof"))
        XCTAssertEqual(fixture.messages[2]["reason"], .string("expired"))
        fixture.result(accepted: true)
        XCTAssertEqual(fixture.statuses.last, .failed)
        fixture.authorizer.hold = true; fixture.authorizer.ignoreCancellation = true
        var next = ceremonyPayload(); next["authenticationId"] = .string("new-id"); next["correlationId"] = .string("new-correlation")
        fixture.message(next)
        try await waitUntil { fixture.authorizer.requests == 2 }
        fixture.message(["type": .string("fido2_abort"), "authenticationId": .string("new-id"), "correlationId": .string("new-correlation")])
        fixture.authorizer.complete()
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(fixture.messages.filter { $0["type"] == .string("fido2_proof") }.count, 1)
        XCTAssertEqual(fixture.statuses.last, .pending)
        fixture.coordinator.suspend()
    }

    func testOversizedAndUnsupportedRequestsNeverReachNativePrompt() async throws {
        let fixture = PasskeyFixture(); try await fixture.ready()
        fixture.coordinator.receive(.message(Data(repeating: 65, count: 14_001)))
        var invalid = ceremonyPayload()
        invalid["options"] = .object(["rpId": .string("auth.example.com"), "challenge": .string("AQID"), "extensions": .object(["prf": .object([:])])])
        fixture.message(invalid)
        try await waitUntil { fixture.messages.count == 2 }
        XCTAssertEqual(fixture.messages.last?["reason"], .string("unsupported_options"))
        XCTAssertEqual(fixture.authorizer.requests, 0)
        fixture.coordinator.suspend()
    }

    func testAbsentDeviceIdentifierIsExplicitNullAndDisabledSendsNothing() async throws {
        let fixture = PasskeyFixture(); try await fixture.ready()
        XCTAssertEqual(fixture.messages.first?["deviceImei"], .null)
        fixture.coordinator.suspend()
        let disabled = PasskeyCoordinator(configuration: .disabled, authorizer: FakePasskeyAuthorizer(), send: { _ in XCTFail() }, status: { _ in XCTFail() })
        disabled.receive(.ready); disabled.receive(.message(Data()))
    }
}

private func ceremonyPayload(expiry: Date = Date().addingTimeInterval(60)) -> [String: JSONValue] {
    ["type": .string("fido2_ceremony"), "authenticationId": .string("Mixed-Case-ID"),
     "action": .number(1), "feature": .string("TransferFunds"), "mode": .string("authenticate"),
     "correlationId": .string("ceremony-correlation"),
     "expiresAt": .string(expiry.ISO8601Format(.init(includingFractionalSeconds: true))),
     "options": .object(["rpId": .string("auth.example.com"), "challenge": .string("AQID"), "userVerification": .string("required")])]
}

@MainActor
private final class PasskeyFixture {
    let authorizer = FakePasskeyAuthorizer()
    var messages: [[String: JSONValue]] = []
    var statuses: [AuthenticationStatus.Status] = []
    var context = ""
    var failSend = false
    lazy var coordinator = PasskeyCoordinator(configuration: .init(relyingPartyIdentifiers: ["auth.example.com"]), authorizer: authorizer,
        send: { [unowned self] data in
            if failSend { throw PasskeyFailure.unavailable }
            messages.append(try JSONDecoder().decode([String: JSONValue].self, from: data))
        }, status: { [unowned self] in statuses.append($0) })
    func ready() async throws {
        let count = messages.count
        coordinator.receive(.ready)
        try await waitUntil { self.messages.count > count }
        context = messages.last?["correlationId"]?.stringValue ?? ""
    }
    func message(_ fields: [String: JSONValue]) {
        var payload = fields
        if payload["correlationId"] == nil { payload["correlationId"] = .string("ceremony-correlation") }
        coordinator.receive(.message(try! JSONEncoder().encode(payload)))
    }
    func result(accepted: Bool) {
        message(["type": .string("fido2_result"), "authenticationId": .string("Mixed-Case-ID"),
                 "accepted": .bool(accepted), "outcome": .string(accepted ? "executed" : "rejected")])
    }
}

@MainActor
private final class FakePasskeyAuthorizer: PasskeyAuthorizing {
    var requests = 0
    var hold = false
    var ignoreCancellation = false
    var failure: PasskeyFailure?
    var continuation: CheckedContinuation<PasskeyProof, Error>?
    var proof: PasskeyProof { .assertion(id: Data([4]), clientData: Data([5]), authenticatorData: Data([6]), signature: Data([7]), userID: Data([8])) }
    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof {
        requests += 1
        if let failure { throw failure }
        if hold { return try await withCheckedThrowingContinuation { continuation = $0 } }
        return proof
    }
    func cancel() {
        guard !ignoreCancellation else { return }
        let pending = continuation; continuation = nil
        pending?.resume(throwing: PasskeyFailure.cancelled)
    }
    func complete() {
        let pending = continuation; continuation = nil; pending?.resume(returning: proof)
    }
}

@MainActor
private func waitUntil(_ condition: () -> Bool) async throws {
    for _ in 0..<500 {
        if condition() { return }
        try await Task.sleep(for: .milliseconds(2))
    }
    XCTFail("Timed out waiting for passkey state transition")
    throw PasskeyFailure.expired
}
