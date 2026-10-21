import Foundation

/// Private wire types never reach chat, tool events or the host's event callback.
struct PasskeyRequest {
    enum Mode: String { case authenticate, register }
    enum UserVerification: String { case required, preferred, discouraged }
    let authenticationID: String
    let correlationID: String
    let mode: Mode
    let relyingParty: String
    let challenge: Data
    let credentialIDs: [Data]
    let userID: Data?
    let userName: String?
    let displayName: String?
    let expiresAt: Date
    let promptTimeout: TimeInterval
    let userVerification: UserVerification

    init(_ payload: [String: JSONValue], configuration: AttentivePasskeyConfiguration, now: Date) throws {
        guard let id = payload["authenticationId"]?.stringValue, !id.isEmpty, id.utf8.count <= 256,
              let correlation = payload["correlationId"]?.stringValue, !correlation.isEmpty, correlation.utf8.count <= 256,
              let mode = Mode(rawValue: payload["mode"]?.stringValue ?? ""),
              let expiryText = payload["expiresAt"]?.stringValue,
              let expiry = Self.expiry(expiryText),
              case .number(let action) = payload["action"], action >= 0, action.rounded() == action,
              let feature = payload["feature"]?.stringValue, !feature.isEmpty, feature.utf8.count <= 128,
              case .object(let options) = payload["options"] else { throw PasskeyFailure.invalidRequest }
        guard expiry > now else { throw PasskeyFailure.expired }
        authenticationID = id
        correlationID = correlation
        self.mode = mode
        expiresAt = expiry
        let permitted: Set<String> = mode == .authenticate
            ? ["challenge", "rpId", "allowCredentials", "userVerification", "timeout", "extensions"]
            : ["challenge", "rp", "user", "pubKeyCredParams", "excludeCredentials", "authenticatorSelection", "attestation", "timeout", "extensions"]
        guard Set(options.keys).isSubset(of: permitted) else { throw PasskeyFailure.unsupportedOptions }
        if let extensions = options["extensions"], extensions != .object([:]) { throw PasskeyFailure.unsupportedOptions }
        challenge = try PasskeyEncoding.decode(options["challenge"]?.stringValue ?? "")
        var timeout = configuration.ceremonyTimeoutSeconds
        if let value = options["timeout"] {
            guard case .number(let milliseconds) = value, milliseconds.isFinite, milliseconds > 0 else { throw PasskeyFailure.invalidRequest }
            timeout = min(timeout, milliseconds / 1000)
        }
        promptTimeout = min(timeout, expiry.timeIntervalSince(now))
        if mode == .authenticate {
            relyingParty = options["rpId"]?.stringValue ?? ""
            userVerification = try Self.verification(options["userVerification"])
            credentialIDs = try Self.credentials(options["allowCredentials"])
            userID = nil; userName = nil; displayName = nil
        } else {
            guard case .object(let rp) = options["rp"], case .object(let user) = options["user"],
                  let name = user["name"]?.stringValue, !name.isEmpty, name.utf8.count <= 256,
                  let display = user["displayName"]?.stringValue, !display.isEmpty, display.utf8.count <= 256,
                  case .array(let parameters) = options["pubKeyCredParams"], !parameters.isEmpty, parameters.count <= 16 else { throw PasskeyFailure.invalidRequest }
            guard Set(rp.keys).isSubset(of: ["id", "name"]), Set(user.keys).isSubset(of: ["id", "name", "displayName"]) else { throw PasskeyFailure.unsupportedOptions }
            let supportsPlatformKey = parameters.contains { parameter in
                guard case .object(let fields) = parameter else { return false }
                return fields["type"] == .string("public-key") && fields["alg"] == .number(-7)
            }
            guard parameters.allSatisfy({ parameter in
                guard case .object(let fields) = parameter,
                      Set(fields.keys).isSubset(of: ["type", "alg"]),
                      fields["type"] == .string("public-key"),
                      case .number(let algorithm) = fields["alg"] else { return false }
                return algorithm.isFinite && algorithm.rounded() == algorithm
            }) else { throw PasskeyFailure.invalidRequest }
            guard supportsPlatformKey, options["attestation"] == nil || options["attestation"] == .string("none") else { throw PasskeyFailure.unsupportedOptions }
            if let selection = options["authenticatorSelection"] {
                guard case .object(let fields) = selection,
                      Set(fields.keys).isSubset(of: ["authenticatorAttachment", "residentKey", "requireResidentKey", "userVerification"]),
                      fields["authenticatorAttachment"] == nil || fields["authenticatorAttachment"] == .string("platform"),
                      fields["residentKey"] == nil || fields["residentKey"] == .string("required"),
                      fields["requireResidentKey"] == nil || fields["requireResidentKey"] == .bool(true)
                else { throw PasskeyFailure.unsupportedOptions }
                // Platform passkeys are discoverable. Other resident-key preferences
                // cannot be selected with this native API and must not be discarded.
                userVerification = try Self.verification(fields["userVerification"])
            } else { userVerification = .preferred }
            relyingParty = rp["id"]?.stringValue ?? ""
            userID = try PasskeyEncoding.decode(user["id"]?.stringValue ?? "", maximum: 64)
            userName = name; displayName = display
            credentialIDs = try Self.credentials(options["excludeCredentials"])
        }
        guard configuration.relyingPartyIdentifiers.contains(where: { $0.lowercased() == relyingParty.lowercased() }),
              AttentivePasskeyConfiguration.isDomain(relyingParty) else { throw PasskeyFailure.untrustedRelyingParty }
    }

    private static func verification(_ value: JSONValue?) throws -> UserVerification {
        guard let value else { return .preferred } // WebAuthn default, not a local policy change.
        guard let text = value.stringValue, let preference = UserVerification(rawValue: text) else {
            throw PasskeyFailure.unsupportedOptions
        }
        return preference
    }

    private static func expiry(_ text: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let value = formatter.date(from: text) { return value }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: text)
    }

    private static func credentials(_ value: JSONValue?) throws -> [Data] {
        guard let value else { return [] }
        guard case .array(let descriptors) = value, descriptors.count <= 64 else { throw PasskeyFailure.invalidRequest }
        return try descriptors.map {
            guard case .object(let fields) = $0, fields["type"] == .string("public-key"),
                  Set(fields.keys).isSubset(of: ["id", "type", "transports"]) else { throw PasskeyFailure.unsupportedOptions }
            if let transports = fields["transports"] {
                guard case .array(let values) = transports, values.allSatisfy({ $0.stringValue != nil }) else { throw PasskeyFailure.invalidRequest }
                guard values.allSatisfy({ $0 == .string("internal") }) else { throw PasskeyFailure.unsupportedOptions }
            }
            return try PasskeyEncoding.decode(fields["id"]?.stringValue ?? "")
        }
    }
}

struct PasskeyProof: CustomStringConvertible, CustomDebugStringConvertible {
    let fields: [String: JSONValue]
    static func assertion(id: Data, clientData: Data, authenticatorData: Data, signature: Data, userID: Data) -> Self {
        .init(fields: credential(id: id, response: [
            "clientDataJSON": .string(PasskeyEncoding.encode(clientData)),
            "authenticatorData": .string(PasskeyEncoding.encode(authenticatorData)),
            "signature": .string(PasskeyEncoding.encode(signature)),
            "userHandle": userID.isEmpty ? .null : .string(PasskeyEncoding.encode(userID))]))
    }
    static func registration(id: Data, clientData: Data, attestation: Data) -> Self {
        .init(fields: credential(id: id, response: [
            "clientDataJSON": .string(PasskeyEncoding.encode(clientData)),
            "attestationObject": .string(PasskeyEncoding.encode(attestation))]))
    }
    private static func credential(id: Data, response: [String: JSONValue]) -> [String: JSONValue] {
        ["id": .string(PasskeyEncoding.encode(id)), "rawId": .string(PasskeyEncoding.encode(id)),
         "type": .string("public-key"),
         "response": .object(response)]
    }
    var description: String { "PasskeyProof(<redacted>)" }
    var debugDescription: String { description }
}
