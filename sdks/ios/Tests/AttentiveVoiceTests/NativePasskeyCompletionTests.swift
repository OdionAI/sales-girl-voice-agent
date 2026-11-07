#if os(iOS)
import AuthenticationServices
import Foundation
import Testing
import UIKit
@testable import AttentiveVoice

@Suite("Native authorization credential retention")
struct NativePasskeyCompletionTests {
    @Test(arguments: [false, true]) @MainActor
    func authorizedCredentialSurvivesWindowLossAndEmitsProof(register: Bool) async throws {
        let device = CompletionDevice()
        var messages: [[String: JSONValue]] = []
        let coordinator = PasskeyCoordinator(configuration: .init(relyingPartyIdentifiers: ["example.com"]),
            authorizer: device, send: { messages.append(try JSONDecoder().decode([String: JSONValue].self, from: $0)) }, status: { _ in })
        coordinator.receive(.ready)
        try await wait { messages.count == 1 }
        coordinator.receive(.message(try JSONEncoder().encode(ceremony(register: register))))
        try await wait { messages.count == 2 }
        #expect(device.anchor == nil)
        #expect(device.window.isHidden)
        let message = try #require(messages.last)
        #expect(message["type"] == .string("fido2_proof"))
        #expect(message["authenticationId"] == .string("Auth-MiXeD"))
        #expect(message["correlationId"] == .string("Correlation-MiXeD"))
        let returnedProof = try #require(device.returnedProof)
        #expect(message["proof"] == .object(returnedProof.fields))
        guard case .object(let proof) = message["proof"], case .object(let response) = proof["response"] else {
            Issue.record("The serialized proof must have a populated response"); return
        }
        let credentialID = try #require(proof["id"]?.stringValue)
        #expect(credentialID.isEmpty == false)
        #expect(response["clientDataJSON"] == .string("Ag"))
        #expect(response[register ? "attestationObject" : "signature"] == .string(register ? "Aw" : "BA"))
        #expect(!messages.contains { $0["type"] == .string("fido2_failed") })
        coordinator.suspend()
    }

    @Test @MainActor
    func cancellationBeforeLateCredentialDoesNotEmitProof() async throws {
        let device = CompletionDevice()
        device.cancelBeforeCompletion = true
        var messages: [[String: JSONValue]] = []
        let coordinator = PasskeyCoordinator(configuration: .init(relyingPartyIdentifiers: ["example.com"]),
            authorizer: device, send: { messages.append(try JSONDecoder().decode([String: JSONValue].self, from: $0)) }, status: { _ in })
        coordinator.receive(.ready)
        try await wait { messages.count == 1 }
        coordinator.receive(.message(try JSONEncoder().encode(ceremony(register: false))))
        try await wait { messages.count == 2 }
        #expect(messages.last?["type"] == .string("fido2_cancelled"))
        #expect(!messages.contains { $0["type"] == .string("fido2_proof") })
        coordinator.suspend()
    }

    @MainActor private func wait(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 {
            if predicate() { return }
            try await Task.sleep(for: .milliseconds(2))
        }
        #expect(predicate())
    }

    private func ceremony(register: Bool) -> [String: JSONValue] {
        ["type": .string("fido2_ceremony"), "authenticationId": .string("Auth-MiXeD"),
         "correlationId": .string("Correlation-MiXeD"), "action": .number(1), "feature": .string("TestOnly"),
         "mode": .string(register ? "register" : "authenticate"), "expiresAt": .string("2099-01-01T00:00:00Z"),
         "options": register
            ? .object(["challenge": .string("AQID"), "rp": .object(["id": .string("example.com")]),
                       "user": .object(["id": .string("AQ"), "name": .string("test"), "displayName": .string("Test")]),
                       "pubKeyCredParams": .array([.object(["type": .string("public-key"), "alg": .number(-7)])])])
            : .object(["challenge": .string("AQID"), "rpId": .string("example.com")])]
    }
}

// Simulates Apple's completion through the same native-session completion method
// used by its delegate. No Apple UI, credentials, bank APIs or real biometrics.
@MainActor private final class CompletionDevice: PasskeyAuthorizing {
    let window = UIWindow()
    var anchor: UIWindow?
    var returnedProof: PasskeyProof?
    var cancelBeforeCompletion = false
    private lazy var native = NativePasskeyAuthorizer { [weak self] in self?.anchor }

    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof {
        anchor = window
        let proof = request.mode == .register
            ? PasskeyProof.registration(id: Data([1]), clientData: Data([2]), attestation: Data([3]))
            : PasskeyProof.assertion(id: Data([1]), clientData: Data([2]), authenticatorData: Data([3]), signature: Data([4]), userID: Data())
        // Enter after UI preflight; reproduce losing the window as Apple completes.
        let result = try await native.beginAuthorization(request, window: window) { controller in
            self.anchor = nil
            self.window.isHidden = true
            if self.cancelBeforeCompletion { self.native.cancel() }
            self.native.completeAuthorizedCredential(proof, for: controller)
        }
        returnedProof = result
        return result
    }
    func cancel() { native.cancel() }
}
#endif
