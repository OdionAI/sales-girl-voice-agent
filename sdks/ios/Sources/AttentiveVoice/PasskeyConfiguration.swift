import Foundation

/// Opt-in native transaction confirmation. No connection or worker configuration is exposed.
public struct AttentivePasskeyConfiguration: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public let relyingPartyIdentifiers: [String]
    /// An opaque identifier supplied by the signed-in host app. The SDK does not read hardware identifiers.
    public let deviceIdentifier: String?
    public let ceremonyTimeoutSeconds: TimeInterval
    public let verificationTimeoutSeconds: TimeInterval
    /// Emit privacy-safe passkey stage/reason logs to the Apple unified log. Off by default.
    public let diagnosticsEnabled: Bool

    public static let disabled = Self(relyingPartyIdentifiers: [])

    public init(relyingPartyIdentifiers: [String], deviceIdentifier: String? = nil,
                ceremonyTimeoutSeconds: TimeInterval = 120, verificationTimeoutSeconds: TimeInterval = 30,
                diagnosticsEnabled: Bool = false) {
        self.relyingPartyIdentifiers = relyingPartyIdentifiers
        self.deviceIdentifier = deviceIdentifier
        self.ceremonyTimeoutSeconds = ceremonyTimeoutSeconds
        self.verificationTimeoutSeconds = verificationTimeoutSeconds
        self.diagnosticsEnabled = diagnosticsEnabled
    }

    var isEnabled: Bool { !relyingPartyIdentifiers.isEmpty }
    func validate() throws {
        guard ceremonyTimeoutSeconds.isFinite, (1...300).contains(ceremonyTimeoutSeconds),
              verificationTimeoutSeconds.isFinite, (1...120).contains(verificationTimeoutSeconds),
              (deviceIdentifier?.utf8.count ?? 0) <= 512,
              relyingPartyIdentifiers.count <= 16,
              relyingPartyIdentifiers.allSatisfy(Self.isDomain) else { throw CallError.invalidRequest }
    }

    static func isDomain(_ value: String) -> Bool {
        guard value.utf8.count <= 253, value.contains("."), !value.hasSuffix(".") else { return false }
        return value.split(separator: ".", omittingEmptySubsequences: false).allSatisfy { label in
            !label.isEmpty && label.utf8.count <= 63 && label.first != "-" && label.last != "-" &&
            label.utf8.allSatisfy { (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45 }
        }
    }

    public var description: String { "AttentivePasskeyConfiguration(\(isEnabled ? "enabled" : "disabled"), deviceIdentifier: <redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["configuration": description]) }
}

enum PasskeyFailure: String, Error {
    case invalidRequest = "invalid_request", unsupportedOptions = "unsupported_options"
    case untrustedRelyingParty = "untrusted_relying_party", expired, busy, replayed
    case unavailable, presentationUnavailable = "presentation_unavailable"
    case cancelled, authorizationFailed = "authorization_failed", invalidCredential = "invalid_credential"
}

enum PasskeyEncoding {
    static func encode(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
    static func decode(_ string: String, maximum: Int = 4096) throws -> Data {
        guard !string.isEmpty, string.utf8.count <= maximum * 2,
              string.utf8.allSatisfy({ (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45 || $0 == 95 }),
              string.count % 4 != 1 else { throw PasskeyFailure.invalidRequest }
        let base = string.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        guard let data = Data(base64Encoded: base + String(repeating: "=", count: (4 - base.count % 4) % 4)),
              data.count <= maximum, encode(data) == string else { throw PasskeyFailure.invalidRequest }
        return data
    }
}
