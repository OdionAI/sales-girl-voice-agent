import Foundation
import Testing
@testable import AttentiveVoice
#if os(iOS)
import AuthenticationServices
#endif

@Suite("FIDO2 Revision 2 contract")
struct FIDO2Revision2Tests {
    @Test @MainActor
    func literalBriefMessagesNeedNoAcknowledgementOrExtraEnvelopeFields() async throws {
        let fixture = Revision2Fixture()
        fixture.coordinator.receive(.ready)
        try await fixture.wait { fixture.messages.count == 1 }
        let context = try #require(fixture.messages.first)
        #expect(Set(context.keys) == ["type", "correlationId", "deviceImei"])
        fixture.coordinator.receive(.ready)
        await Task.yield()
        #expect(fixture.messages.count == 1)
        // Literal brief schema: no version, contextId or acknowledgement exchange.
        fixture.receive(try #require(#"{"type":"fido2_ceremony","authenticationId":"Wema-MiXeD","correlationId":"Case-Sensitive-Request","action":1,"feature":"TransferFunds","mode":"authenticate","expiresAt":"2099-10-21T12:34:56Z","options":{"rpId":"auth.example.com","challenge":"AQID","userVerification":"required"}}"#.data(using: .utf8)))
        try await fixture.wait { fixture.messages.count == 2 }
        let proof = try #require(fixture.messages.last)
        #expect(Set(proof.keys) == ["type", "authenticationId", "correlationId", "proof"])
        #expect(proof["authenticationId"] == .string("Wema-MiXeD"))
        #expect(proof["correlationId"] == .string("Case-Sensitive-Request"))
        #expect(fixture.statuses.last == .pending)
        fixture.receive(Data(#"{"type":"fido2_result","authenticationId":"Wema-MiXeD","correlationId":"wrong","accepted":true}"#.utf8))
        fixture.receive(Data(#"{"type":"fido2_result","authenticationId":"Wema-MiXeD","correlationId":"Case-Sensitive-Request","status":"verified","authenticated":true}"#.utf8))
        #expect(fixture.statuses.last == .pending)
        fixture.receive(Data(#"{"type":"fido2_result","authenticationId":"Wema-MiXeD","correlationId":"Case-Sensitive-Request","accepted":true,"outcome":"failed"}"#.utf8))
        // Authentication acceptance is separate from transaction outcome.
        #expect(fixture.statuses.last == .verified)
        fixture.coordinator.suspend()
    }

    @Test @MainActor
    func rejectedProofCannotBeAuthorizedByAnExecutedOutcome() async throws {
        let fixture = Revision2Fixture()
        fixture.coordinator.receive(.ready)
        try await fixture.wait { fixture.messages.count == 1 }
        fixture.receive(try JSONEncoder().encode(revision2Payload()))
        try await fixture.wait { fixture.messages.count == 2 }
        fixture.receive(Data(#"{"type":"fido2_result","authenticationId":"Wema-MiXeD","correlationId":"Case-Sensitive-Request","accepted":false,"outcome":"executed"}"#.utf8))
        #expect(fixture.statuses.last == .failed)
        #expect(!fixture.statuses.contains(.verified))
        fixture.coordinator.suspend()
    }

    @Test(arguments: ["2099-10-21T12:34:56Z", "2099-10-21T12:34:56.123Z", "2099-10-21T13:34:56+01:00"])
    func iso8601ExpiryPreservesTheInstant(_ expiry: String) throws {
        var payload = revision2Payload(); payload["expiresAt"] = .string(expiry)
        let request = try PasskeyRequest(payload, configuration: revision2Configuration, now: Date())
        let expected = ISO8601DateFormatter().date(from: "2099-10-21T12:34:56Z")!
        #expect(abs(request.expiresAt.timeIntervalSince(expected)) < 0.124)
    }

    @Test(arguments: [JSONValue.number(1900000000), .string("not-a-date"), .string("2026-10-21"), .string("")])
    func malformedOrLegacyExpiryIsRejected(_ expiry: JSONValue) {
        var payload = revision2Payload(); payload["expiresAt"] = expiry
        #expect(throws: (any Error).self) { try PasskeyRequest(payload, configuration: revision2Configuration, now: Date()) }
    }

    @Test @MainActor
    func anAlreadyExpiredCeremonyDoesNotOpenAPromptOrChangeThePreviousResult() async throws {
        let fixture = Revision2Fixture()
        fixture.coordinator.receive(.ready)
        try await fixture.wait { fixture.messages.count == 1 }
        fixture.receive(try JSONEncoder().encode(revision2Payload()))
        try await fixture.wait { fixture.messages.count == 2 }
        fixture.receive(Data(#"{"type":"fido2_result","authenticationId":"Wema-MiXeD","correlationId":"Case-Sensitive-Request","accepted":true}"#.utf8))
        #expect(fixture.statuses.last == .verified)
        var payload = revision2Payload()
        payload["authenticationId"] = .string("expired-id")
        payload["correlationId"] = .string("expired-correlation")
        payload["expiresAt"] = .string("2000-01-01T00:00:00Z")
        fixture.receive(try JSONEncoder().encode(payload))
        await Task.yield()
        #expect(fixture.authorizer.requests == 1)
        #expect(fixture.messages.count == 2)
        #expect(fixture.statuses.last == .verified)
        fixture.coordinator.suspend()
    }

    @Test(arguments: ["residentKey", "requireResidentKey", "authenticatorAttachment"])
    func unsupportedRegistrationPreferencesAreNotSilentlyDropped(_ field: String) {
        let unsupported: [String: JSONValue] = ["residentKey": .string("discouraged"), "requireResidentKey": .bool(false), "authenticatorAttachment": .string("cross-platform")]
        var payload = revision2Payload(register: true)
        guard case .object(var options) = payload["options"] else { return }
        options["authenticatorSelection"] = .object([field: unsupported[field]!])
        payload["options"] = .object(options)
        #expect(throws: PasskeyFailure.unsupportedOptions) { try PasskeyRequest(payload, configuration: revision2Configuration, now: Date()) }
    }

    @Test(arguments: ["required", "preferred", "discouraged"])
    func userVerificationIsPreservedForBothModes(_ preference: String) throws {
        for register in [false, true] {
            var payload = revision2Payload(register: register)
            guard case .object(var options) = payload["options"] else { return }
            if register {
                options["authenticatorSelection"] = .object(["userVerification": .string(preference), "residentKey": .string("required")])
            } else { options["userVerification"] = .string(preference) }
            payload["options"] = .object(options)
            let request = try PasskeyRequest(payload, configuration: revision2Configuration, now: Date())
            #expect(request.userVerification.rawValue == preference)
        }
    }

    @Test
    func platformUnsupportedTransportHintsFailExplicitly() {
        var payload = revision2Payload()
        guard case .object(var options) = payload["options"] else { return }
        options["allowCredentials"] = .array([.object(["type": .string("public-key"), "id": .string("BAU"), "transports": .array([.string("usb")])])])
        payload["options"] = .object(options)
        #expect(throws: PasskeyFailure.unsupportedOptions) { try PasskeyRequest(payload, configuration: revision2Configuration, now: Date()) }
    }

    #if os(iOS)
    @Test @MainActor
    func nativeTypedRequestsCarryTheSupportedWebAuthnOptions() throws {
        let authorizer = NativePasskeyAuthorizer { nil }
        for preference in ["required", "preferred", "discouraged"] {
            var payload = revision2Payload()
            payload["options"] = .object(["rpId": .string("auth.example.com"), "challenge": .string("AQID"),
                "userVerification": .string(preference), "timeout": .number(5000),
                "allowCredentials": .array([.object(["type": .string("public-key"), "id": .string("BAU")])])])
            let request = try PasskeyRequest(payload, configuration: revision2Configuration, now: Date())
            let native = try #require(try authorizer.authorizationRequest(for: request) as? ASAuthorizationPlatformPublicKeyCredentialAssertionRequest)
            #expect(native.challenge == Data([1, 2, 3]))
            #expect(native.relyingPartyIdentifier == "auth.example.com")
            #expect(native.allowedCredentials.map(\.credentialID) == [Data([4, 5])])
            #expect(native.userVerificationPreference.rawValue == preference)
            #expect(request.promptTimeout == 5)

            payload = revision2Payload(register: true)
            guard case .object(var options) = payload["options"] else { return }
            options["authenticatorSelection"] = .object(["authenticatorAttachment": .string("platform"), "residentKey": .string("required"), "requireResidentKey": .bool(true), "userVerification": .string(preference)])
            if #available(iOS 17.4, *) {
                options["excludeCredentials"] = .array([.object(["type": .string("public-key"), "id": .string("Bg")])])
            }
            payload["options"] = .object(options)
            let registrationRequest = try PasskeyRequest(payload, configuration: revision2Configuration, now: Date())
            let registration = try #require(try authorizer.authorizationRequest(for: registrationRequest) as? ASAuthorizationPlatformPublicKeyCredentialRegistrationRequest)
            #expect(registration.challenge == Data([1, 2, 3]))
            #expect(registration.userID == Data([4, 5]))
            #expect(registration.name == "customer")
            #expect(registration.displayName == "Customer")
            #expect(registration.userVerificationPreference.rawValue == preference)
            #expect(registration.attestationPreference == .none)
            if #available(iOS 17.4, *) { #expect(registration.excludedCredentials?.map(\.credentialID) == [Data([6])]) }
        }
    }
    #endif
}

private let revision2Configuration = AttentivePasskeyConfiguration(relyingPartyIdentifiers: ["auth.example.com"])

private func revision2Payload(register: Bool = false) -> [String: JSONValue] {
    ["type": .string("fido2_ceremony"), "authenticationId": .string("Wema-MiXeD"),
     "correlationId": .string("Case-Sensitive-Request"), "action": .number(1), "feature": .string("TransferFunds"),
     "mode": .string(register ? "register" : "authenticate"), "expiresAt": .string("2099-10-21T12:34:56Z"),
     "options": register ? .object(["rp": .object(["id": .string("auth.example.com"), "name": .string("Example")]),
        "user": .object(["id": .string("BAU"), "name": .string("customer"), "displayName": .string("Customer")]),
        "challenge": .string("AQID"), "pubKeyCredParams": .array([.object(["type": .string("public-key"), "alg": .number(-7)])])])
        : .object(["rpId": .string("auth.example.com"), "challenge": .string("AQID")])]
}

@MainActor
private final class Revision2Fixture {
    let authorizer = Revision2Authorizer()
    var messages: [[String: JSONValue]] = []
    var statuses: [AuthenticationStatus.Status] = []
    lazy var coordinator = PasskeyCoordinator(configuration: revision2Configuration, authorizer: authorizer,
        send: { [unowned self] in messages.append(try JSONDecoder().decode([String: JSONValue].self, from: $0)) },
        status: { [unowned self] in statuses.append($0) })
    func receive(_ data: Data) { coordinator.receive(.message(data)) }
    func wait(_ condition: () -> Bool) async throws {
        for _ in 0..<500 {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(2))
        }
        Issue.record("Timed out waiting for Revision 2 message")
        throw PasskeyFailure.expired
    }
}

@MainActor
private final class Revision2Authorizer: PasskeyAuthorizing {
    var requests = 0
    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof {
        requests += 1
        return .assertion(id: Data([1]), clientData: Data([2]), authenticatorData: Data([3]), signature: Data([4]), userID: Data())
    }
    func cancel() {}
}
