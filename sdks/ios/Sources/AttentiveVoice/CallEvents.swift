import Foundation

public enum CallState: String, Sendable {
    case idle, connecting, connected, reconnecting, ending, ended, failed
}

public enum AgentState: String, Sendable {
    case waiting, initializing, listening, thinking, speaking, disconnected
}

public struct Transcript: Identifiable, Equatable, Sendable {
    public enum Speaker: String, Sendable { case caller, agent }
    public let id: String
    public let speaker: Speaker
    public let text: String
    public let isFinal: Bool
}

public enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue]), array([JSONValue]), string(String), number(Double), bool(Bool), null

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let bool = try? value.decode(Bool.self) { self = .bool(bool) }
        else if let string = try? value.decode(String.self) { self = .string(string) }
        else if let number = try? value.decode(Double.self) { self = .number(number) }
        else if let object = try? value.decode([String: JSONValue].self) { self = .object(object) }
        else { self = .array(try value.decode([JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .object(let object): try value.encode(object)
        case .array(let array): try value.encode(array)
        case .string(let string): try value.encode(string)
        case .number(let number): try value.encode(number)
        case .bool(let bool): try value.encode(bool)
        case .null: try value.encodeNil()
        }
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }
}

public struct ToolActivity: Identifiable, Equatable, Sendable {
    public let id: String
    public let toolName: String
    public let event: String
    public let status: String
    public let payload: [String: JSONValue]
}

public struct AuthenticationStatus: Equatable, Sendable {
    public enum Scope: Sendable { case session, action }
    public enum Status: String, Sendable { case pending, verified, failed }
    public let scope: Scope
    public let status: Status
}

public enum CallEvent: Equatable, Sendable {
    case stateChanged(CallState)
    case agentStateChanged(AgentState)
    case microphoneChanged(Bool)
    case transcript(Transcript)
    case toolActivity(ToolActivity)
    case authentication(AuthenticationStatus)
    case actionOutcome([String: JSONValue])
    case metrics([String: JSONValue])
    /// First non-silent decoded agent audio in this call, before device playback.
    case agentAudioReceived
    case failure(CallError)
}

enum BackendEventDecoder {
    static func decode(data: Data, topic: String) -> CallEvent? {
        guard data.count <= 65_536,
              let payload = try? JSONDecoder().decode([String: JSONValue].self, from: data) else { return nil }
        switch topic {
        case "odion.tool.activity":
            guard let id = payload["call_id"]?.stringValue, !id.isEmpty,
                  let name = payload["tool_name"]?.stringValue else { return nil }
            return .toolActivity(.init(id: id, toolName: name,
                event: payload["event"]?.stringValue ?? "",
                status: payload["status"]?.stringValue ?? "", payload: payload))
        case "odion.auth.status", "odion.auth.action_status":
            let raw = payload["status"]?.stringValue ?? "pending"
            // Display only. Tool authorization stays on the backend.
            let status: AuthenticationStatus.Status = raw == "failed" ? .failed :
                (raw == "verified" || payload["authenticated"] == .bool(true) ? .verified : .pending)
            return .authentication(.init(scope: topic == "odion.auth.status" ? .session : .action, status: status))
        case "odion.auth.action": return .actionOutcome(payload)
        case "odion.voice_lab.metrics": return .metrics(payload)
        default: return nil
        }
    }
}
