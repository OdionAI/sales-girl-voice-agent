import Foundation

/// Display metadata from the dashboard. Prompts, tools and routing stay on the server.
public struct AgentConfiguration: Decodable, Equatable, Sendable {
    public let agentId: String
    public let agentName: String
    public let title: String
    public let callerRequired: Bool
}

struct SDKClient: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    private let apiKey: String
    private let agentID: String
    private let customerID: String?
    private let session: URLSession
    private let baseURL: URL

    // Tests can inject a URLSession, but customer apps do not supply service endpoints.
    init(apiKey: String, agentID: String, customerID: String? = nil, session: URLSession? = nil,
         baseURL: URL = URL(string: "https://attentive.odion.ai/api/sdk/v1/")!) {
        self.apiKey = apiKey
        self.agentID = agentID
        self.customerID = customerID
        self.baseURL = baseURL
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.httpCookieStorage = nil
        self.session = session ?? URLSession(configuration: config, delegate: NoCallRedirects(), delegateQueue: nil)
    }

    var description: String { "Attentive SDK client (credentials redacted)" }
    var debugDescription: String { description }

    func configuration() async throws -> AgentConfiguration {
        let value: AgentConfiguration = try await post("configuration", callerToken: nil)
        guard value.agentId == agentID, !value.title.isEmpty, !value.agentName.isEmpty else {
            throw CallError.invalidResponse
        }
        return value
    }

    func credentials(callerToken: String?) async throws -> CallCredentials {
        let credentials: CallCredentials = try await post("calls", callerToken: callerToken)
        guard !credentials.roomName.isEmpty, !credentials.participantToken.isEmpty else { throw CallError.invalidResponse }
        try validateURL(credentials.serverUrl, schemes: ["wss"], allowInsecure: false)
        return credentials
    }

    func validate() throws {
        guard apiKey.range(of: #"^att_pk_[a-zA-Z0-9_-]{43}$"#, options: .regularExpression) != nil else {
            throw CallError.invalidAPIKey
        }
        guard agentID.range(of: #"^agt_[a-zA-Z0-9_-]{1,80}$"#, options: .regularExpression) != nil else {
            throw CallError.invalidRequest
        }
        if let customerID,
           customerID.range(of: #"^[a-zA-Z0-9_-]{1,64}$"#, options: .regularExpression) != customerID.startIndex..<customerID.endIndex {
            throw CallError.invalidRequest
        }
    }

    private func post<Value: Decodable>(_ path: String, callerToken: String?) async throws -> Value {
        try validate()
        if let callerToken, callerToken.isEmpty || callerToken.utf8.count > 8192 { throw CallError.invalidRequest }
        let endpoint = baseURL.appendingPathComponent(path)
        try validateURL(endpoint, schemes: ["https"], allowInsecure: false)
        var http = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        http.httpMethod = "POST"
        http.setValue("application/json", forHTTPHeaderField: "Content-Type")
        http.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        var body = ["agentId": agentID]
        if path == "calls", let customerID { body["customerId"] = customerID }
        if let callerToken { body["callerToken"] = callerToken }
        http.httpBody = try JSONEncoder().encode(body)
        let data = try await callServiceData(http, session: session)
        guard let value = try? JSONDecoder().decode(Value.self, from: data) else { throw CallError.invalidResponse }
        return value
    }
}
