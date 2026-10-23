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
    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof {
        requests += 1
        if let error { throw error }
        return .assertion(id: Data([1]), clientData: Data("private-proof".utf8), authenticatorData: Data([2]), signature: Data([3]), userID: Data())
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
    func receive(challenge: String = "AQID") {
        let payload: [String: JSONValue] = ["type": .string("fido2_ceremony"),
            "authenticationId": .string("private-auth-id"), "correlationId": .string("private-correlation"),
            "action": .number(1), "feature": .string("private-feature"), "mode": .string("authenticate"),
            "expiresAt": .string(Date().addingTimeInterval(60).ISO8601Format()),
            "options": .object(["rpId": .string("example.com"), "challenge": .string(challenge)])]
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
