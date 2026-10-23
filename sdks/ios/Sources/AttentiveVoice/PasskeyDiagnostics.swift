import Foundation
#if canImport(os)
import os
#endif

/// Only bounded labels and numeric system error codes can enter diagnostics.
/// Never accepts request payloads, identifiers, domains, descriptions or proofs.
@MainActor
final class PasskeyDiagnostics {
    enum Stage: String { case configuration, connection, context, receipt, validation, presentation, proof, verification, cleanup }
    enum Outcome: String { case started, requested, succeeded, failed, ignored, cancelled }
    enum Check: String { case envelope, options, challenge, credentials, registrationIdentity, algorithms, authenticatorSelection, relyingParty }
    struct Entry: Equatable, CustomStringConvertible {
        let stage: Stage
        let outcome: Outcome
        let reason: PasskeyFailure?
        let check: Check?
        let systemError: PasskeySystemError?
        var description: String {
            var fields = ["passkey", "stage=\(stage.rawValue)", "outcome=\(outcome.rawValue)"]
            if let reason { fields.append("reason=\(reason.rawValue)") }
            if let check { fields.append("check=\(check.rawValue)") }
            if let systemError { fields += ["system_domain=\(systemError.domain.rawValue)", "system_code=\(systemError.code)"] }
            return fields.joined(separator: " ")
        }
    }
    let enabled: Bool
    private let sink: ((Entry) -> Void)?
    private(set) var check: Check?
    private(set) var lastFailure: Entry?
    #if canImport(os)
    private let logger = Logger(subsystem: "ai.odion.attentive", category: "Passkeys")
    #endif

    init(enabled: Bool, sink: ((Entry) -> Void)? = nil) { self.enabled = enabled; self.sink = sink }

    func checking(_ value: Check) {
        check = value
        record(.validation, .started, check: value)
    }

    func record(_ stage: Stage, _ outcome: Outcome, reason: PasskeyFailure? = nil,
                check: Check? = nil, systemError: PasskeySystemError? = nil) {
        guard enabled else { return }
        if stage == .receipt && outcome == .started { self.check = nil; lastFailure = nil }
        let entry = Entry(stage: stage, outcome: outcome, reason: reason, check: check, systemError: systemError)
        if outcome == .failed { lastFailure = entry }
        #if canImport(os)
        if outcome == .failed { logger.error("\(entry.description, privacy: .public)") }
        else { logger.notice("\(entry.description, privacy: .public)") }
        #endif
        sink?(entry)
    }

    var failureMetadata: [String: JSONValue] {
        guard enabled, let lastFailure else { return [:] }
        var fields: [String: JSONValue] = ["stage": .string(lastFailure.stage.rawValue)]
        if let check = lastFailure.check { fields["check"] = .string(check.rawValue) }
        if let systemError = lastFailure.systemError {
            fields["systemDomain"] = .string(systemError.domain.rawValue)
            fields["systemCode"] = .number(Double(systemError.code))
        }
        return ["diagnostics": .object(fields)]
    }
}

struct PasskeySystemError: Equatable, Sendable {
    enum Domain: String, Sendable { case authorization, localAuthentication, other }
    let domain: Domain
    let code: Int
    init(_ error: NSError) {
        switch error.domain {
        case "com.apple.AuthenticationServices.AuthorizationError": domain = .authorization
        case "com.apple.LocalAuthentication": domain = .localAuthentication
        default: domain = .other
        }
        code = error.code
    }
}

struct PasskeyNativeError: Error {
    let failure: PasskeyFailure
    let system: PasskeySystemError
}
