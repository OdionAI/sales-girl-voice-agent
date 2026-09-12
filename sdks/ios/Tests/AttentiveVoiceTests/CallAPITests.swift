import Foundation
import XCTest
@testable import AttentiveVoice

final class CallAPITests: XCTestCase {
    func testExistingResponseAndRequestContract() async throws {
        let provider = try makeProvider(path: "success")
        let credentials = try await provider.fetch(for: request)
        XCTAssertEqual(credentials.roomName, "room")
        XCTAssertEqual(credentials.serverUrl.absoluteString, "wss://rtc.example.com")
    }

    func testStatusErrorDoesNotLeakResponse() async throws {
        let provider = try makeProvider(path: "402")
        do { _ = try await provider.fetch(for: request); XCTFail() }
        catch {
            XCTAssertEqual(error as? CallError, .httpStatus(402))
            XCTAssertFalse(error.localizedDescription.contains("private-provider-detail"))
            XCTAssertFalse(error.localizedDescription.contains("402"))
        }
    }

    func testInsufficientCreditsProvidesTypedAmountsAndSafeMessage() async throws {
        let provider = try makeProvider(path: "no-airtime")
        do { _ = try await provider.fetch(for: request); XCTFail() }
        catch {
            XCTAssertEqual(error as? CallError, .insufficientCredits(balanceKobo: 0, requiredMinimumKobo: 167))
            XCTAssertTrue(error.localizedDescription.contains("call credit"))
            XCTAssertTrue(error.localizedDescription.contains("dashboard"))
            XCTAssertFalse(error.localizedDescription.contains("private-provider-detail"))
            XCTAssertFalse(error.localizedDescription.contains("402"))
        }
    }

    func testMissingOrMalformedBillingAmountsKeepKnownError() async throws {
        for path in ["no-airtime-missing", "no-airtime-invalid"] {
            let provider = try makeProvider(path: path)
            do { _ = try await provider.fetch(for: request); XCTFail(path) }
            catch { XCTAssertEqual(error as? CallError, .insufficientCredits(balanceKobo: nil, requiredMinimumKobo: nil)) }
        }
    }

    func testUnknownOrUnusablePaymentResponsesAreSafe() async throws {
        for path in ["payment-unknown", "payment-html", "payment-oversized"] {
            let provider = try makeProvider(path: path)
            do { _ = try await provider.fetch(for: request); XCTFail(path) }
            catch { XCTAssertEqual(error as? CallError, .httpStatus(402)) }
        }
    }

    func testBillingCodeDoesNotOverrideOtherHTTPStatuses() async throws {
        let provider = try makeProvider(path: "billing-code-on-500")
        do { _ = try await provider.fetch(for: request); XCTFail() }
        catch { XCTAssertEqual(error as? CallError, .httpStatus(500)) }
    }

    func testInvalidAndInsecureCredentialsRejected() async throws {
        for (path, expected) in [("malformed", CallError.invalidResponse), ("insecure", .insecureURL), ("empty", .invalidResponse)] {
            let provider = try makeProvider(path: path)
            do { _ = try await provider.fetch(for: request); XCTFail(path) }
            catch { XCTAssertEqual(error as? CallError, expected) }
        }
    }

    func testInvalidInputRejectedBeforeNetwork() async throws {
        let provider = try makeProvider(path: "success")
        var empty = request
        empty.businessSlug = "   "
        do { _ = try await provider.fetch(for: empty); XCTFail() }
        catch { XCTAssertEqual(error as? CallError, .invalidRequest) }
    }

    private var request: CallRequest {
        .init(businessSlug: "business", agentPublicId: "agt_test", endUserContact: "caller@example.com",
              profile: .init(customerId: "TEST_CUSTOMER", phoneNumber: "08000000000"), toolWaitSpeechMode: .toolSpecific)
    }

    private func makeProvider(path: String) throws -> HTTPCallCredentialProvider {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        return try HTTPCallCredentialProvider(endpoint: URL(string: "https://example.com/\(path)")!, session: URLSession(configuration: config))
    }
}

private final class MockURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.lastPathComponent
        let status = path == "billing-code-on-500" ? 500 :
            (path == "402" || path.hasPrefix("no-airtime") || path.hasPrefix("payment-") ? 402 : 200)
        let data: Data
        switch path {
        case "malformed": data = Data("not-json".utf8)
        case "402": data = Data(#"{"detail":"private-provider-detail"}"#.utf8)
        case "no-airtime", "billing-code-on-500":
            data = Data(#"{"code":"no_airtime","detail":"private-provider-detail","authorized":false,"balance_kobo":0,"required_min_kobo":167}"#.utf8)
        case "no-airtime-missing": data = Data(#"{"code":"no_airtime"}"#.utf8)
        case "no-airtime-invalid":
            data = Data(#"{"code":"no_airtime","balance_kobo":-1,"required_min_kobo":"invalid"}"#.utf8)
        case "payment-unknown": data = Data(#"{"code":"other_payment_error","detail":"private-provider-detail"}"#.utf8)
        case "payment-html": data = Data("<html>private-provider-detail</html>".utf8)
        case "payment-oversized":
            data = Data((#"{"code":"no_airtime","detail":""# + String(repeating: "x", count: 65_536) + #""}"#).utf8)
        default:
            let scheme = path == "insecure" ? "ws" : "wss"
            data = try! JSONSerialization.data(withJSONObject: [
                "serverUrl": "\(scheme)://rtc.example.com", "roomName": "room",
                "participantToken": path == "empty" ? "" : "secret", "participantName": "user", "extra": "ignored",
            ])
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
