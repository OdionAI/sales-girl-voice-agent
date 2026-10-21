import Foundation

enum PasskeyTransportEvent { case ready, unavailable, message(Data) }

@MainActor
protocol PasskeyAuthorizing: AnyObject {
    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof
    func cancel()
}

@MainActor
final class UnavailablePasskeyAuthorizer: PasskeyAuthorizing {
    func authorize(_ request: PasskeyRequest) async throws -> PasskeyProof { throw PasskeyFailure.unavailable }
    func cancel() {}
}

/// One ceremony per call at a time. Only a correlated server result can mark an action verified.
@MainActor
final class PasskeyCoordinator {
    private enum Phase { case prompting, awaitingVerification }
    private let configuration: AttentivePasskeyConfiguration
    private let authorizer: any PasskeyAuthorizing
    private let send: (Data) async throws -> Void
    private let status: (AuthenticationStatus.Status) -> Void
    private var connectionID: UUID?
    private var active: (token: UUID, request: PasskeyRequest, phase: Phase)?
    private var seen: Set<String> = []
    private var seenCorrelations: Set<String> = []
    private var work: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var contextWork: Task<Void, Never>?

    init(configuration: AttentivePasskeyConfiguration, authorizer: any PasskeyAuthorizing,
         send: @escaping (Data) async throws -> Void, status: @escaping (AuthenticationStatus.Status) -> Void) {
        self.configuration = configuration; self.authorizer = authorizer; self.send = send; self.status = status
    }

    func receive(_ event: PasskeyTransportEvent) {
        guard configuration.isEnabled else { return }
        switch event {
        case .ready:
            guard connectionID == nil else { return }
            let connection = UUID()
            connectionID = connection
            contextWork = Task { [weak self] in
                guard let self else { return }
                do {
                    try await self.emit(type: "session_context", correlation: connection.uuidString, additional: [
                        "deviceImei": self.configuration.deviceIdentifier.map(JSONValue.string) ?? .null])
                } catch {
                    guard self.connectionID == connection else { return }
                    self.suspend()
                    self.status(.failed)
                }
            }
        case .unavailable: suspend()
        case .message(let data): handle(data)
        }
    }

    /// Call on disconnect/reconnect and call end. No credential response is queued for replay.
    func suspend() {
        guard configuration.isEnabled else { return }
        connectionID = nil
        active = nil
        work?.cancel(); work = nil
        timeout?.cancel(); timeout = nil
        contextWork?.cancel(); contextWork = nil
        authorizer.cancel()
        status(.pending)
    }

    func reset() { suspend(); seen.removeAll(); seenCorrelations.removeAll() }

    private func handle(_ data: Data) {
        guard connectionID != nil, data.count <= 14_000,
              let payload = try? JSONDecoder().decode([String: JSONValue].self, from: data),
              payload["version"] == nil || payload["version"] == .number(1),
              let type = payload["type"]?.stringValue,
              let id = payload["authenticationId"]?.stringValue, !id.isEmpty, id.utf8.count <= 256,
              let correlation = payload["correlationId"]?.stringValue,
              !correlation.isEmpty, correlation.utf8.count <= 256 else { return }
        if type == "fido2_result" {
            guard let active, active.request.authenticationID == id, active.request.correlationID == correlation,
                  active.phase == .awaitingVerification, active.request.expiresAt > Date() else { return }
            guard case .bool(let accepted) = payload["accepted"] else { return }
            finish(accepted ? .verified : .failed)
            return
        }
        if type == "fido2_abort" {
            guard active?.request.authenticationID == id, active?.request.correlationID == correlation else { return }
            finish(.pending)
            return
        }
        guard type == "fido2_ceremony" else { return }
        // A duplicate of the active request must not dismiss or reopen the native sheet.
        if active?.request.authenticationID.lowercased() == id.lowercased() { return }
        guard !seen.contains(id.lowercased()), !seenCorrelations.contains(correlation) else {
            reject(id, correlation, .replayed); return
        }
        guard seen.count < 128 else { reject(id, correlation, .unavailable); return }
        seen.insert(id.lowercased()); seenCorrelations.insert(correlation)
        guard active == nil else { reject(id, correlation, .busy); return }
        do {
            let request = try PasskeyRequest(payload, configuration: configuration, now: Date())
            let token = UUID()
            active = (token, request, .prompting)
            status(.pending)
            armTimeout(seconds: request.promptTimeout, token: token)
            work = Task { [weak self] in
                guard let self else { return }
                do {
                    let proof = try await self.authorizer.authorize(request)
                    guard !Task.isCancelled, self.active?.token == token else { return }
                    guard request.expiresAt > Date() else { throw PasskeyFailure.expired }
                    self.active?.phase = .awaitingVerification
                    self.armTimeout(seconds: min(self.configuration.verificationTimeoutSeconds,
                                                  request.expiresAt.timeIntervalSinceNow), token: token)
                    try await self.emit(type: "fido2_proof", id: id, correlation: correlation, additional: ["proof": .object(proof.fields)])
                    // Publishing is not verification. Remain pending until fido2_result.
                } catch {
                    guard self.active?.token == token, !Task.isCancelled else { return }
                    let failure = error as? PasskeyFailure ?? .authorizationFailed
                    let cancelled = failure == .cancelled
                    self.finish(cancelled ? .pending : .failed)
                    self.reject(id, correlation, failure, cancelled: cancelled)
                }
            }
        } catch {
            // Already expired ceremonies are ignored without changing a previous result.
            guard error as? PasskeyFailure != .expired else { return }
            status(.failed); reject(id, correlation, error as? PasskeyFailure ?? .invalidRequest)
        }
    }

    private func armTimeout(seconds: TimeInterval, token: UUID) {
        timeout?.cancel()
        timeout = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(max(0, seconds))) } catch { return }
            guard let self, let active = self.active, active.token == token else { return }
            let id = active.request.authenticationID
            self.finish(.failed)
            self.reject(id, active.request.correlationID, .expired)
        }
    }

    private func finish(_ value: AuthenticationStatus.Status) {
        active = nil
        timeout?.cancel(); timeout = nil
        work?.cancel(); work = nil
        authorizer.cancel()
        status(value)
    }

    private func reject(_ id: String, _ correlation: String, _ reason: PasskeyFailure, cancelled: Bool = false) {
        let connection = connectionID
        Task { [weak self] in
            guard let self, let connection, self.connectionID == connection else { return }
            try? await self.emit(type: cancelled ? "fido2_cancelled" : "fido2_failed", id: id, correlation: correlation,
                                 additional: cancelled ? [:] : ["reason": .string(reason.rawValue)])
        }
    }

    private func emit(type: String, id: String? = nil, correlation: String, additional: [String: JSONValue] = [:]) async throws {
        guard connectionID != nil else { throw PasskeyFailure.unavailable }
        var payload = additional
        // The private topic carries the protocol version. No extra handshake is required.
        payload["type"] = .string(type); payload["correlationId"] = .string(correlation)
        if let id { payload["authenticationId"] = .string(id) }
        let data = try JSONEncoder().encode(payload)
        guard data.count <= 14_000 else { throw PasskeyFailure.invalidCredential }
        try Task.checkCancellation()
        try await send(data)
    }
}
