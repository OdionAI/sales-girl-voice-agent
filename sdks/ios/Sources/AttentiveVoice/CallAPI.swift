import Foundation

public struct CallerProfile: Codable, Equatable, Sendable {
    public var customerId: String
    public var accountNumber: String?
    public var phoneNumber: String?

    public init(customerId: String, accountNumber: String? = nil, phoneNumber: String? = nil) {
        self.customerId = customerId
        self.accountNumber = accountNumber
        self.phoneNumber = phoneNumber
    }
}

public enum ToolWaitSpeechMode: String, Codable, Sendable {
    case toolSpecific = "tool_specific"
    case llmGenerated = "llm_generated"
}

public struct CallRequest: Codable, Equatable, Sendable {
    public var businessSlug: String
    public var agentPublicId: String
    public var endUserContact: String
    public var language: String
    public var wemaContext: CallerProfile?
    public var toolWaitSpeechMode: ToolWaitSpeechMode?

    public init(businessSlug: String, agentPublicId: String, endUserContact: String,
                language: String = "en", profile: CallerProfile? = nil,
                toolWaitSpeechMode: ToolWaitSpeechMode? = nil) {
        self.businessSlug = businessSlug
        self.agentPublicId = agentPublicId
        self.endUserContact = endUserContact
        self.language = language
        self.wemaContext = profile
        self.toolWaitSpeechMode = toolWaitSpeechMode
    }

    func validate() throws {
        guard [businessSlug, agentPublicId, endUserContact, language].allSatisfy({
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }) else { throw CallError.invalidRequest }
    }
}

/// Only credential-provider implementations need this type; never log or persist it.
public struct CallCredentials: Decodable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let serverUrl: URL
    public let roomName: String
    public let participantToken: String

    public init(serverUrl: URL, roomName: String, participantToken: String) {
        self.serverUrl = serverUrl
        self.roomName = roomName
        self.participantToken = participantToken
    }

    public var description: String { "CallCredentials(<redacted>)" }
    public var debugDescription: String { description }
}

public protocol CallCredentialProvider: Sendable {
    func fetch(for request: CallRequest) async throws -> CallCredentials
}

public enum CallError: Error, LocalizedError, Equatable, Sendable {
    case invalidAPIKey, agentNotAllowed, callerRequired, callerSessionExpired, callLimitReached, serviceUnavailable
    case customerMismatch
    case invalidRequest, insecureURL, invalidResponse, alreadyActive, notConnected
    case microphoneDenied, connectionFailed, agentUnavailable, messageFailed
    /// Platform call credit, not the caller's bank balance. Amounts are optional, in NGN kobo.
    case insufficientCredits(balanceKobo: Int?, requiredMinimumKobo: Int?)
    case httpStatus(Int)

    public var errorDescription: String? {
        switch self {
        case .invalidAPIKey: return "The calling key is invalid or revoked. Check your dashboard integration settings."
        case .agentNotAllowed: return "This calling key does not have access to the selected agent."
        case .callerRequired: return "Sign in to your account before starting this call."
        case .callerSessionExpired: return "Your caller session has expired. Please sign in again."
        case .customerMismatch: return "The customer ID does not match the signed-in customer. Please sign in again."
        case .callLimitReached: return "The calling limit has been reached. Please try again later."
        case .serviceUnavailable: return "Calling is temporarily unavailable. Please try again later."
        case .invalidRequest: return "The call request is incomplete or invalid."
        case .insecureURL: return "A secure endpoint is required. Enable insecure connections only for local development."
        case .invalidResponse: return "The call service returned invalid connection details."
        case .alreadyActive: return "A call is already active or closing."
        case .notConnected: return "The call is not connected."
        case .microphoneDenied: return "Microphone access is required. Enable it in Settings."
        case .connectionFailed: return "The call connection failed. End this call before trying again."
        case .agentUnavailable: return "The agent did not become available. Please try again."
        case .messageFailed: return "The message could not be sent."
        case .insufficientCredits:
            return "This agent's account does not have enough call credit. Please ask the account owner to top up in the Attentive dashboard, then try again."
        case .httpStatus(402):
            return "Calling is unavailable because payment is required. Please ask the account owner to check billing in the Attentive dashboard."
        case .httpStatus(let status): return "The call service returned HTTP \(status)."
        }
    }
}

func validateURL(_ url: URL, schemes: [String], allowInsecure: Bool) throws {
    let scheme = url.scheme?.lowercased() ?? ""
    guard let host = url.host, !host.isEmpty, url.user == nil, url.password == nil,
          url.fragment == nil, schemes.contains(scheme),
          allowInsecure || scheme == "https" || scheme == "wss" else {
        throw CallError.insecureURL
    }
}

public struct HTTPCallCredentialProvider: CallCredentialProvider {
    private let endpoint: URL
    private let allowInsecure: Bool
    private let session: URLSession

    public init(endpoint: URL, allowsInsecureDevelopmentConnections: Bool = false,
                session: URLSession? = nil) throws {
        try validateURL(endpoint, schemes: ["https", "http"], allowInsecure: allowsInsecureDevelopmentConnections)
        self.endpoint = endpoint
        self.allowInsecure = allowsInsecureDevelopmentConnections
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        self.session = session ?? URLSession(configuration: config, delegate: NoCallRedirects(), delegateQueue: nil)
    }

    public func fetch(for request: CallRequest) async throws -> CallCredentials {
        try request.validate()
        var http = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        http.httpMethod = "POST"
        http.setValue("application/json", forHTTPHeaderField: "Content-Type")
        http.httpBody = try JSONEncoder().encode(request)
        let data = try await callServiceData(http, session: session, allowInsecure: allowInsecure)
        guard let credentials = try? JSONDecoder().decode(CallCredentials.self, from: data),
              !credentials.participantToken.isEmpty, !credentials.roomName.isEmpty else {
            throw CallError.invalidResponse
        }
        try validateURL(credentials.serverUrl, schemes: ["wss", "ws"], allowInsecure: allowInsecure)
        return credentials
    }
}

func callServiceData(_ http: URLRequest, session: URLSession, allowInsecure: Bool = false) async throws -> Data {
    let (data, response) = try await session.data(for: http)
    guard let response = response as? HTTPURLResponse else { throw CallError.invalidResponse }
    if let finalURL = response.url {
        try validateURL(finalURL, schemes: ["https", "http"], allowInsecure: allowInsecure)
        guard finalURL == http.url else { throw CallError.invalidResponse }
    }
    guard data.count <= 65_536 else {
        if response.statusCode != 200 { throw CallError.httpStatus(response.statusCode) }
        throw CallError.invalidResponse
    }
    guard response.statusCode == 200 else {
        if let failure = try? JSONDecoder().decode(CallServiceFailure.self, from: data) {
            switch (response.statusCode, failure.code) {
            case (402, "no_airtime"):
                throw CallError.insufficientCredits(balanceKobo: failure.balanceKobo,
                                                    requiredMinimumKobo: failure.requiredMinimumKobo)
            case (401, "invalid_api_key"): throw CallError.invalidAPIKey
            case (403, "agent_not_allowed"): throw CallError.agentNotAllowed
            case (403, "caller_required"): throw CallError.callerRequired
            case (403, "customer_mismatch"): throw CallError.customerMismatch
            case (401, "invalid_caller_token"): throw CallError.callerSessionExpired
            case (429, "call_limit"): throw CallError.callLimitReached
            case (503, "sdk_unavailable"), (503, "service_unavailable"): throw CallError.serviceUnavailable
            default: break
            }
        }
        throw CallError.httpStatus(response.statusCode)
    }
    return data
}

// Read only known service fields; never display arbitrary upstream error details.
private struct CallServiceFailure: Decodable {
    let code: String
    let balanceKobo: Int?
    let requiredMinimumKobo: Int?

    private enum CodingKeys: String, CodingKey {
        case code, balanceKobo = "balance_kobo", requiredMinimumKobo = "required_min_kobo"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        code = try values.decode(String.self, forKey: .code)
        let balance = try? values.decode(Int.self, forKey: .balanceKobo)
        let minimum = try? values.decode(Int.self, forKey: .requiredMinimumKobo)
        balanceKobo = balance.flatMap { $0 >= 0 ? $0 : nil }
        requiredMinimumKobo = minimum.flatMap { $0 >= 0 ? $0 : nil }
    }
}

// Call/enrollment requests contain private caller data. Do not replay redirects.
final class NoCallRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
