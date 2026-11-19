import Foundation
import XCTest
@testable import AttentiveVoice

final class SDKClientTests: XCTestCase {
    private let key = "att_pk_" + String(repeating: "a", count: 43)

    func testConfigurationAndCredentialsOnlySendKeyAgentAndOptionalToken() async throws {
        let client = makeClient()
        let configuration = try await client.configuration()
        XCTAssertEqual(configuration.title, "Talk to Example Bank")
        XCTAssertEqual(configuration.agentName, "Example")
        XCTAssertNil(configuration.themeKey)
        _ = try await client.credentials(callerToken: nil)
        _ = try await client.credentials(callerToken: "signed-caller-token")
        XCTAssertFalse(client.description.contains(key))
    }

    func testCustomerIDIsSentOnlyForCallsWithoutLoggingIt() async throws {
        let client = makeClient("customer", customerID: "CUSTOMER_1")
        _ = try await client.configuration()
        _ = try await client.credentials(callerToken: "signed-caller-token")
        XCTAssertFalse(client.description.contains("CUSTOMER_1"))
    }

    func testMalformedCustomerIDsAreRejectedBeforeNetworking() async {
        for value in ["", " ", " customer ", "customer@example.test", String(repeating: "a", count: 65), "test\nother"] {
            do { _ = try await makeClient(customerID: value).configuration(); XCTFail(value) }
            catch { XCTAssertEqual(error as? CallError, .invalidRequest) }
        }
    }

    func testCallerPhoneIsOnlySentOnCalls() async throws {
        for (path, number) in [("phone-local", "08161540638"), ("phone-international", "+2348161540638")] {
            let client = makeClient(path, customerID: "CUSTOMER_1", phoneNumber: number)
            _ = try await client.configuration()
            _ = try await client.credentials(callerToken: "signed-caller-token")
            XCTAssertFalse(client.description.contains(number))
            XCTAssertFalse(client.debugDescription.contains(number))
        }
    }

    func testMalformedPhoneNumbersAreRejectedBeforeNetworking() async {
        for value in ["", "0816154063", "081615406380", "+012345678", "+1234567",
                      "+1234567890123456", "08161540638\n", " 08161540638",
                      "081-6154-0638", "٠٨١٦١٥٤٠٦٣٨"] {
            do { _ = try await makeClient(phoneNumber: value).credentials(callerToken: nil); XCTFail("Invalid number accepted") }
            catch { XCTAssertEqual(error as? CallError, .invalidRequest) }
        }
    }

    func testValidPhoneBoundariesAndUnspecifiedPhone() throws {
        for value: String? in [nil, "01234567890", "+12345678", "+123456789012345"] {
            XCTAssertNoThrow(try makeClient(phoneNumber: value).validate())
        }
    }

    func testServerSecretAndMalformedKeysRejected() async {
        for invalid in ["att_sk_" + String(repeating: "a", count: 43), "", key + "\r\nX-Header: value"] {
            do { _ = try await SDKClient(apiKey: invalid, agentID: "agt_example").configuration(); XCTFail() }
            catch { XCTAssertEqual(error as? CallError, .invalidAPIKey) }
        }
    }

    func testKeyErrorsAreTypedAndDoNotEchoBackendText() async {
        for (path, expected) in [("invalid_api_key", CallError.invalidAPIKey), ("agent_not_allowed", .agentNotAllowed),
            ("caller_required", .callerRequired), ("customer_mismatch", .customerMismatch),
            ("invalid_caller_token", .callerSessionExpired), ("call_limit", .callLimitReached),
            ("sdk_unavailable", .serviceUnavailable), ("service_unavailable", .serviceUnavailable)] {
            do { _ = try await makeClient(path).credentials(callerToken: nil); XCTFail(path) }
            catch {
                XCTAssertEqual(error as? CallError, expected)
                XCTAssertFalse(error.localizedDescription.contains("private-backend"))
            }
        }
    }

    func testWrongAgentAndInsecureTransportRejected() async {
        do { _ = try await makeClient("wrong-agent").configuration(); XCTFail() }
        catch { XCTAssertEqual(error as? CallError, .invalidResponse) }
        do { _ = try await makeClient("insecure").credentials(callerToken: nil); XCTFail() }
        catch { XCTAssertEqual(error as? CallError, .insecureURL) }
    }

    func testEmptyCallerTokenRejected() async {
        do { _ = try await makeClient().credentials(callerToken: ""); XCTFail() }
        catch { XCTAssertEqual(error as? CallError, .invalidRequest) }
    }

    func testDashboardThemeIsDecodedWhenPresent() async throws {
        let configuration = try await makeClient("theme").configuration()
        XCTAssertEqual(configuration.themeKey, "theme_3")
    }

    func testAnimatedDashboardThemeKeysReachCallerConfigurationUnchanged() async throws {
        let grain = try await makeClient("animated-grain").configuration()
        let smooth = try await makeClient("animated-smooth").configuration()
        XCTAssertEqual(grain.themeKey, "animated:#2F6FED:grain")
        XCTAssertEqual(smooth.themeKey, "animated:#A1B2C3:smooth")
    }

    private func makeClient(_ path: String = "success", customerID: String? = nil, phoneNumber: String? = nil) -> SDKClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [SDKProtocol.self]
        return SDKClient(apiKey: key, agentID: "agt_example", customerID: customerID, phoneNumber: phoneNumber, session: URLSession(configuration: config),
                         baseURL: URL(string: "https://attentive.odion.ai/\(path)/")!)
    }
}

private final class SDKProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer att_pk_" + String(repeating: "a", count: 43))
        XCTAssertNil(request.value(forHTTPHeaderField: "X-API-Key"))
        XCTAssertNil(request.value(forHTTPHeaderField: "X-Service-Token"))
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
        let body = try! JSONSerialization.jsonObject(with: data) as! [String: String]
        XCTAssertEqual(body["agentId"], "agt_example")
        XCTAssertTrue(Set(body.keys).isSubset(of: ["agentId", "callerToken", "customerId", "phoneNumber"]))
        let path = request.url!.pathComponents[1]
        if path.hasPrefix("phone-") && request.url!.lastPathComponent == "calls" {
            XCTAssertEqual(body["phoneNumber"], path == "phone-local" ? "08161540638" : "+2348161540638")
            XCTAssertEqual(body["customerId"], "CUSTOMER_1")
            XCTAssertEqual(body["callerToken"], "signed-caller-token")
        } else {
            XCTAssertNil(body["phoneNumber"])
        }
        if path == "customer" && request.url!.lastPathComponent == "calls" {
            XCTAssertEqual(body["customerId"], "CUSTOMER_1")
            XCTAssertEqual(body["callerToken"], "signed-caller-token")
        } else if !path.hasPrefix("phone-") || request.url!.lastPathComponent != "calls" {
            XCTAssertNil(body["customerId"])
        }
        let errorStatuses = ["invalid_api_key": 401, "agent_not_allowed": 403, "caller_required": 403,
                             "customer_mismatch": 403,
                             "invalid_caller_token": 401, "call_limit": 429, "sdk_unavailable": 503, "service_unavailable": 503]
        var response: [String: Any]
        if errorStatuses[path] != nil { response = ["code": path, "message": "private-backend"] }
        else if request.url!.lastPathComponent == "configuration" {
            response = ["agentId": path == "wrong-agent" ? "agt_other" : "agt_example",
                        "agentName": "Example", "title": "Talk to Example Bank",
                        "callerRequired": false]
            if path == "theme" { response["themeKey"] = "theme_3" }
            if path == "animated-grain" { response["themeKey"] = "animated:#2F6FED:grain" }
            if path == "animated-smooth" { response["themeKey"] = "animated:#A1B2C3:smooth" }
        } else {
            response = ["serverUrl": path == "insecure" ? "ws://rtc.example.test" : "wss://rtc.example.test",
                        "roomName": "room", "participantToken": "test-token"]
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: errorStatuses[path] ?? 200,
            httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: response))
        client?.urlProtocolDidFinishLoading(self)
    }
}
