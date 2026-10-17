import Foundation

/// A pending listening-settings handshake must not override a later mute request.
/// Preparation can wait independently; actual track changes are serialized in intent order.
@MainActor
final class MicrophoneActivation {
    private var revision = 0
    private var closed = false
    private var publication: Task<Void, Error>?
    private let prepareEnable: () async throws -> Void
    private let publish: (Bool) async throws -> Void
    private let didApply: (Bool) throws -> Void

    init(prepareEnable: @escaping () async throws -> Void,
         publish: @escaping (Bool) async throws -> Void,
         didApply: @escaping (Bool) throws -> Void) {
        self.prepareEnable = prepareEnable
        self.publish = publish
        self.didApply = didApply
    }

    func setEnabled(_ enabled: Bool) async throws {
        try Task.checkCancellation()
        guard !closed else { throw CancellationError() }
        revision += 1
        let request = revision
        do {
            if enabled { try await prepareEnable() }
            try Task.checkCancellation()
            guard !closed else { throw CancellationError() }
            guard request == revision else { return }
            let previous = publication
            let task = Task<Void, Error> { [self] in
                _ = try? await previous?.value
                try Task.checkCancellation()
                guard !closed else { throw CancellationError() }
                guard request == revision else { return }
                try await publish(enabled)
                try Task.checkCancellation()
                guard !closed else { throw CancellationError() }
                guard request == revision else { return }
                try didApply(enabled)
            }
            publication = task
            defer { if request == revision { publication = nil } }
            try await withTaskCancellationHandler(operation: { try await task.value }, onCancel: { task.cancel() })
        } catch {
            // Superseding an initial enable with mute is successful, not a failed call.
            if !closed, request != revision { return }
            throw error
        }
    }

    func close() {
        closed = true
        revision += 1
        publication?.cancel()
    }
}
