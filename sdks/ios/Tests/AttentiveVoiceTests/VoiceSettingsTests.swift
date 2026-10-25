import Foundation
import Testing
@testable import AttentiveVoice

struct VoiceSettingsTests {
    @Test(arguments: VoiceAccent.allCases, VoiceGender.allCases)
    func requestRoundTripsExactVoiceKeys(accent: VoiceAccent, gender: VoiceGender) throws {
        let request = CallRequest(businessSlug: "business", agentPublicId: "agt_example",
                                  endUserContact: "caller@example.test", language: "fr",
                                  voiceLanguage: accent, voiceGender: gender)
        let data = try JSONEncoder().encode(request)
        let body = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(body["voiceLanguage"] as? String == accent.rawValue)
        #expect(body["voiceGender"] as? String == gender.rawValue)
        #expect(body["language"] as? String == "fr")
        #expect(try JSONDecoder().decode(CallRequest.self, from: data) == request)
    }

    @Test func omittedPreferencesPreserveExistingPayloadAndDecodeOldRequests() throws {
        let request = CallRequest(businessSlug: "business", agentPublicId: "agt_example",
                                  endUserContact: "caller@example.test")
        #expect(request.voiceLanguage == nil)
        #expect(request.voiceGender == nil)
        let body = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: String])
        #expect(body == ["businessSlug": "business", "agentPublicId": "agt_example",
                         "endUserContact": "caller@example.test", "language": "en"])
        let old = Data(#"{"businessSlug":"business","agentPublicId":"agt_example","endUserContact":"caller@example.test","language":"en"}"#.utf8)
        #expect(try JSONDecoder().decode(CallRequest.self, from: old) == request)
    }

    @Test(arguments: [#""voiceLanguage":"en-GB""#, #""voiceGender":"unknown""#])
    func invalidWireEnumValuesAreRejected(field: String) {
        let data = Data((#"{"businessSlug":"business","agentPublicId":"agt_example","endUserContact":"caller@example.test","language":"en","# + field + "}").utf8)
        #expect(throws: DecodingError.self) { try JSONDecoder().decode(CallRequest.self, from: data) }
    }

    @Test(arguments: VoiceAccent.allCases, VoiceGender.allCases)
    func bothHTTPPathsForwardVoicePreferences(accent: VoiceAccent, gender: VoiceGender) async throws {
        let session = makeSession()
        let provider = try HTTPCallCredentialProvider(endpoint: URL(string: "https://example.test/connection-details")!, session: session)
        let request = CallRequest(businessSlug: "business", agentPublicId: "agt_example",
                                  endUserContact: "caller@example.test", language: "fr",
                                  voiceLanguage: accent, voiceGender: gender)
        let advanced = try await provider.fetch(for: request)
        let advancedBody = try body(from: advanced)
        #expect(advancedBody["voiceLanguage"] == accent.rawValue)
        #expect(advancedBody["voiceGender"] == gender.rawValue)
        #expect(advancedBody["language"] == "fr")

        let client = SDKClient(apiKey: "att_pk_" + String(repeating: "a", count: 43), agentID: "agt_example",
                               customerID: "CUSTOMER_1", voiceLanguage: accent, voiceGender: gender,
                               session: session, baseURL: URL(string: "https://example.test/")!)
        _ = try await client.configuration() // Fixture rejects voice fields on configuration requests.
        let dashboard = try await client.credentials(callerToken: "signed-caller-token")
        let dashboardBody = try body(from: dashboard)
        #expect(dashboardBody == ["agentId": "agt_example", "customerId": "CUSTOMER_1",
                                  "callerToken": "signed-caller-token",
                                  "voiceLanguage": accent.rawValue, "voiceGender": gender.rawValue])
    }

    @Test func dashboardOmitsUnsetPreferencesIndependently() async throws {
        for (accent, gender) in [(nil, nil), (VoiceAccent.enNG, nil), (nil, VoiceGender.male)] {
            let client = SDKClient(apiKey: "att_pk_" + String(repeating: "a", count: 43), agentID: "agt_example",
                                   voiceLanguage: accent, voiceGender: gender, session: makeSession(),
                                   baseURL: URL(string: "https://example.test/")!)
            let credentials = try await client.credentials(callerToken: nil)
            let payload = try body(from: credentials)
            #expect(payload["voiceLanguage"] == accent?.rawValue)
            #expect(payload["voiceGender"] == gender?.rawValue)
            #expect(Set(payload.keys).isSubset(of: ["agentId", "voiceLanguage", "voiceGender"]))
        }
    }

    private func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [VoiceSettingsProtocol.self]
        return URLSession(configuration: config)
    }

    private func body(from credentials: CallCredentials) throws -> [String: String] {
        let data = try #require(Data(base64Encoded: credentials.roomName))
        return try #require(JSONSerialization.jsonObject(with: data) as? [String: String])
    }
}

// Echo only fixture request bytes in the credential response, without shared mutable capture state.
private final class VoiceSettingsProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        do {
            var data = request.httpBody ?? Data()
            if let stream = request.httpBodyStream {
                stream.open()
                defer { stream.close() }
                var buffer = [UInt8](repeating: 0, count: 1024)
                while stream.hasBytesAvailable {
                    let count = stream.read(&buffer, maxLength: buffer.count)
                    if count <= 0 { break }
                    data.append(buffer, count: count)
                }
            }
            let body = try JSONSerialization.jsonObject(with: data) as? [String: String]
            let configuration = request.url?.lastPathComponent == "configuration"
            let valid = !configuration || body == ["agentId": "agt_example"]
            let response: [String: Any] = configuration
                ? ["agentId": "agt_example", "agentName": "Example", "title": "Example", "callerRequired": false]
                : ["serverUrl": "wss://example.test", "roomName": data.base64EncodedString(), "participantToken": "test-token"]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: valid ? 200 : 400,
                httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try JSONSerialization.data(withJSONObject: response))
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
}
