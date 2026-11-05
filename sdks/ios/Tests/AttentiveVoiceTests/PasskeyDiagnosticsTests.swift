import Foundation
import Testing
@testable import AttentiveVoice

@Suite("Privacy-safe passkey diagnostics")
struct PasskeyDiagnosticsTests {
    @Test @MainActor
    func disabledIsSilentAndDoesNotChangeFailureMessages() async throws {
        #expect(!AttentivePasskeyConfiguration.disabled.diagnosticsEnabled)
        let fixture = DiagnosticFixture(enabled: false)
        await fixture.ready()
        fixture.receive(challenge: "not canonical==")
        try await fixture.waitForMessage()
        #expect(fixture.entries.isEmpty)
        #expect(fixture.messages.last?["diagnostics"] == nil)
        #expect(fixture.messages.last?["reason"] == .string("invalid_request"))
        fixture.coordinator.suspend()
    }

    @Test @MainActor
    func validationFailureIdentifiesTheCheckWithoutLoggingPayloads() async throws {
        let fixture = DiagnosticFixture()
        await fixture.ready()
        fixture.receive(challenge: "private-challenge==")
        try await fixture.waitForMessage()
        #expect(fixture.authorizer.requests == 0)
        let failure = try #require(fixture.entries.last { $0.outcome == .failed })
        #expect(failure.stage == .validation)
        #expect(failure.check == .challenge)
        #expect(failure.reason == .invalidRequest)
        #expect(fixture.messages.last?["diagnostics"] == .object(["stage": .string("validation"), "check": .string("challenge")]))
        fixture.assertRedacted()
        fixture.coordinator.suspend()
    }

    @Test @MainActor
    func systemErrorsKeepOnlyNumericCodeAndKnownDomain() async throws {
        let fixture = DiagnosticFixture()
        let error = NSError(domain: "com.apple.AuthenticationServices.AuthorizationError", code: 1004,
                            userInfo: [NSLocalizedDescriptionKey: "private-device private-proof private-challenge"])
        fixture.authorizer.error = PasskeyNativeError(failure: .authorizationFailed, system: .init(error))
        await fixture.ready(); fixture.receive()
        try await fixture.waitForMessage()
        let failure = try #require(fixture.entries.last { $0.outcome == .failed })
        #expect(failure.stage == .presentation)
        #expect(failure.systemError?.domain == .authorization)
        #expect(failure.systemError?.code == 1004)
        #expect(fixture.messages.last?["diagnostics"] == .object(["stage": .string("presentation"),
            "systemDomain": .string("authorization"), "systemCode": .number(1004)]))
        let unknown = PasskeySystemError(NSError(domain: "private-device.example", code: -1))
        #expect(unknown.domain == .other)
        fixture.assertRedacted()
        fixture.coordinator.suspend()
    }

    @Test @MainActor
    func successfulProofRemainsPendingUntilBackendVerification() async throws {
        let fixture = DiagnosticFixture()
        await fixture.ready(); fixture.receive()
        try await fixture.waitForMessage()
        #expect(fixture.statuses.last == .pending)
        #expect(fixture.entries.contains { $0.stage == .proof && $0.outcome == .succeeded })
        #expect(fixture.entries.contains { $0.stage == .verification && $0.outcome == .started })
        fixture.coordinator.receive(.message(try JSONEncoder().encode([
            "type": JSONValue.string("fido2_result"), "authenticationId": .string("private-auth-id"),
            "correlationId": .string("private-correlation"), "accepted": .bool(true)])))
        #expect(fixture.statuses.last == .verified)
        #expect(fixture.entries.last?.stage == .verification)
        fixture.assertRedacted()
        fixture.coordinator.suspend()
    }

    @Test(arguments: [false, true]) @MainActor
    func successfulAuthorizationEmitsPopulatedProofWithOriginalIDs(register: Bool) async throws {
        let fixture = DiagnosticFixture()
        let expected = register
            ? PasskeyProof.registration(id: Data([1]), clientData: Data([2]), attestation: Data([3]))
            : PasskeyProof.assertion(id: Data([1]), clientData: Data([2]), authenticatorData: Data([3]), signature: Data([4]), userID: Data())
        fixture.authorizer.proof = expected
        await fixture.ready(); fixture.receive(register: register)
        try await fixture.waitForMessage()
        let message = try #require(fixture.messages.last)
        #expect(message["type"] == .string("fido2_proof"))
        #expect(message["authenticationId"] == .string("private-auth-id"))
        #expect(message["correlationId"] == .string("private-correlation"))
        #expect(message["proof"] == .object(expected.fields))
        guard case .object(let proof) = message["proof"] else { Issue.record("Expected a populated proof object"); return }
        #expect(!proof.isEmpty)
        let credentialID = try #require(proof["id"]?.stringValue)
        #expect(credentialID.isEmpty == false)
        guard case .object(let response) = proof["response"] else { Issue.record("Expected a proof response object"); return }
        let clientData = try #require(response["clientDataJSON"]?.stringValue)
        let credentialData = try #require(response[register ? "attestationObject" : "signature"]?.stringValue)
        #expect(clientData.isEmpty == false)
        #expect(credentialData.isEmpty == false)
        #expect(!fixture.messages.contains { $0["type"] == .string("fido2_failed") })
        #expect(fixture.statuses.last == .pending)
        fixture.coordinator.suspend()
    }

    @Test(arguments: ["empty", "missingResponse", "emptySignature", "emptyAttestation"]) @MainActor
    func incompleteProofIsRejectedBeforeSending(kind: String) async throws {
        let fixture = DiagnosticFixture()
        switch kind {
        case "empty": fixture.authorizer.proof = .init(fields: [:])
        case "missingResponse": fixture.authorizer.proof = .init(fields: ["id": .string("AQ"), "type": .string("public-key")])
        case "emptySignature": fixture.authorizer.proof = .assertion(id: Data([1]), clientData: Data([2]), authenticatorData: Data([3]), signature: Data(), userID: Data())
        default: fixture.authorizer.proof = .registration(id: Data([1]), clientData: Data([2]), attestation: Data())
        }
        await fixture.ready(); fixture.receive(register: kind == "emptyAttestation")
        try await fixture.waitForMessage()
        #expect(!fixture.messages.contains { $0["type"] == .string("fido2_proof") })
        #expect(fixture.messages.last?["reason"] == .string("invalid_credential"))
        #expect(fixture.messages.last?["diagnostics"] == .object(["stage": .string("proof")]))
        fixture.coordinator.suspend()
    }

    @Test(arguments: [PasskeyDiagnostics.Check.presentationAnchor, .foregroundScene, .visibleWindow]) @MainActor
    func coordinatorPreservesOriginalPresentationFailure(check: PasskeyDiagnostics.Check) async throws {
        let fixture = DiagnosticFixture()
        fixture.authorizer.beforeResult = {
            fixture.diagnostics.record(.presentation, .failed, reason: .presentationUnavailable, check: check)
        }
        fixture.authorizer.error = PasskeyFailure.presentationUnavailable
        await fixture.ready(); fixture.receive()
        try await fixture.waitForMessage()
        #expect(fixture.messages.last?["type"] == .string("fido2_failed"))
        #expect(fixture.messages.last?["reason"] == .string("presentation_unavailable"))
        #expect(fixture.messages.last?["diagnostics"] == .object(["stage": .string("presentation"), "check": .string(check.rawValue)]))
        #expect(!fixture.messages.contains { $0["type"] == .string("fido2_proof") })
        fixture.coordinator.suspend()
    }

    @Test @MainActor
    func aDifferentFailureDoesNotInheritAnEarlierCheck() async throws {
        let fixture = DiagnosticFixture()
        fixture.authorizer.beforeResult = {
            fixture.diagnostics.record(.presentation, .failed, reason: .presentationUnavailable, check: .presentationAnchor)
        }
        fixture.authorizer.error = PasskeyFailure.invalidCredential
        await fixture.ready(); fixture.receive()
        try await fixture.waitForMessage()
        #expect(fixture.messages.last?["reason"] == .string("invalid_credential"))
        #expect(fixture.messages.last?["diagnostics"] == .object(["stage": .string("presentation")]))
        fixture.coordinator.suspend()
    }

    @Test @MainActor
    func malformedEnvelopeIsVisibleWithoutEchoingUntrustedData() {
        var entries: [PasskeyDiagnostics.Entry] = []
        let diagnostics = PasskeyDiagnostics(enabled: true, sink: { entries.append($0) })
        let coordinator = PasskeyCoordinator(configuration: .init(relyingPartyIdentifiers: ["example.com"]),
            authorizer: DiagnosticAuthorizer(), diagnostics: diagnostics, send: { _ in }, status: { _ in })
        coordinator.receive(.message(Data("private-proof-not-json".utf8)))
        #expect(entries.last?.stage == .receipt)
        #expect(entries.last?.outcome == .ignored)
        #expect(!entries.map(\.description).joined().contains("private-proof"))
    }
}

@MainActor
private final class DiagnosticAuthorizer: PasskeyAuthorizing {
    var requests = 0
    var error: (any Error)?
    var beforeResult: (() -> Void)?
    var proof = PasskeyProof.assertion(id: Data([1]), clientData: Data("private-proof".utf8), authenticatorData: Data([2]), signature: Data([3]), userID: Data())
    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof {
        requests += 1
        beforeResult?()
        if let error { throw error }
        return proof
    }
    func cancel() {}
}

@MainActor
private final class DiagnosticFixture {
    var entries: [PasskeyDiagnostics.Entry] = []
    var messages: [[String: JSONValue]] = []
    var statuses: [AuthenticationStatus.Status] = []
    let enabled: Bool
    let authorizer = DiagnosticAuthorizer()
    lazy var diagnostics = PasskeyDiagnostics(enabled: enabled, sink: { [weak self] in self?.entries.append($0) })
    lazy var coordinator = PasskeyCoordinator(
        configuration: .init(relyingPartyIdentifiers: ["example.com"], deviceIdentifier: "private-device", diagnosticsEnabled: enabled),
        authorizer: authorizer, diagnostics: diagnostics,
        send: { [weak self] in self?.messages.append(try JSONDecoder().decode([String: JSONValue].self, from: $0)) },
        status: { [weak self] in self?.statuses.append($0) })
    init(enabled: Bool = true) { self.enabled = enabled }
    func ready() async {
        coordinator.receive(.ready)
        for _ in 0..<100 { if messages.count == 1 { return }; await Task.yield() }
    }
    func receive(challenge: String = "AQID", register: Bool = false) {
        let payload: [String: JSONValue] = ["type": .string("fido2_ceremony"),
            "authenticationId": .string("private-auth-id"), "correlationId": .string("private-correlation"),
            "action": .number(1), "feature": .string("private-feature"), "mode": .string(register ? "register" : "authenticate"),
            "expiresAt": .string(Date().addingTimeInterval(60).ISO8601Format()),
            "options": register
                ? .object(["challenge": .string(challenge), "rp": .object(["id": .string("example.com")]),
                           "user": .object(["id": .string("AQ"), "name": .string("test"), "displayName": .string("Test")]),
                           "pubKeyCredParams": .array([.object(["type": .string("public-key"), "alg": .number(-7)])])])
                : .object(["rpId": .string("example.com"), "challenge": .string(challenge)])]
        coordinator.receive(.message(try! JSONEncoder().encode(payload)))
    }
    func waitForMessage() async throws {
        for _ in 0..<100 { if messages.count >= 2 { return }; try await Task.sleep(for: .milliseconds(2)) }
        #expect(messages.count >= 2)
    }
    func assertRedacted() {
        let logs = entries.map(\.description).joined(separator: "\n")
        for forbidden in ["private-", "example.com", "AQID", "deviceImei", "authenticationId", "signature"] {
            #expect(!logs.contains(forbidden))
        }
    }
}
