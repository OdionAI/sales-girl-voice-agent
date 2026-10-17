import Foundation
import Testing
@testable import AttentiveVoice

@Suite("Microphone activation intent")
@MainActor
struct MicrophoneActivationTests {
    @Test
    func muteSupersedesInitialEnableWaitingForListeningAcknowledgement() async throws {
        var preparation: CheckedContinuation<Void, Error>?
        var publications: [Bool] = []
        var applied: [Bool] = []
        let activation = MicrophoneActivation(prepareEnable: {
            try await withCheckedThrowingContinuation { preparation = $0 }
        }, publish: { publications.append($0) }, didApply: { applied.append($0) })
        let initialEnable = Task { try await activation.setEnabled(true) }
        await eventually { preparation != nil }
        try await activation.setEnabled(false)
        #expect(publications == [false])
        #expect(applied == [false])
        preparation?.resume()
        try await initialEnable.value
        #expect(publications == [false])
        #expect(applied == [false])
    }

    @Test
    func supersededEnableFailureDoesNotFailConnection() async throws {
        var preparation: CheckedContinuation<Void, Error>?
        var applied: [Bool] = []
        let activation = MicrophoneActivation(prepareEnable: {
            try await withCheckedThrowingContinuation { preparation = $0 }
        }, publish: { _ in }, didApply: { applied.append($0) })
        let initialEnable = Task { try await activation.setEnabled(true) }
        await eventually { preparation != nil }
        try await activation.setEnabled(false)
        preparation?.resume(throwing: AttentiveAudioInputError.timedOut)
        try await initialEnable.value
        #expect(applied == [false])
    }

    @Test
    func muteDuringPublicationIsAppliedLastWithoutStaleUnmuteCallback() async throws {
        var publication: CheckedContinuation<Void, Error>?
        var publications: [Bool] = []
        var applied: [Bool] = []
        let activation = MicrophoneActivation(prepareEnable: {}, publish: { enabled in
            publications.append(enabled)
            if enabled { try await withCheckedThrowingContinuation { publication = $0 } }
        }, didApply: { applied.append($0) })
        let initialEnable = Task { try await activation.setEnabled(true) }
        await eventually { publication != nil }
        let mute = Task { try await activation.setEnabled(false) }
        await Task.yield()
        #expect(publications == [true])
        publication?.resume()
        try await initialEnable.value
        try await mute.value
        #expect(publications == [true, false])
        #expect(applied == [false])
    }

    @Test
    func closeDuringPreparationCannotPublishMicrophoneLater() async throws {
        var preparation: CheckedContinuation<Void, Error>?
        var publications: [Bool] = []
        let activation = MicrophoneActivation(prepareEnable: {
            try await withCheckedThrowingContinuation { preparation = $0 }
        }, publish: { publications.append($0) }, didApply: { _ in })
        let initialEnable = Task { try await activation.setEnabled(true) }
        await eventually { preparation != nil }
        activation.close()
        preparation?.resume()
        do { try await initialEnable.value; Issue.record("Expected closed activation cancellation") }
        catch { #expect(error is CancellationError) }
        #expect(publications.isEmpty)
    }

    @Test
    func cancellingEnableDoesNotPublishAfterPreparationFinishes() async throws {
        var preparation: CheckedContinuation<Void, Error>?
        var publications: [Bool] = []
        let activation = MicrophoneActivation(prepareEnable: {
            try await withCheckedThrowingContinuation { preparation = $0 }
        }, publish: { publications.append($0) }, didApply: { _ in })
        let initialEnable = Task { try await activation.setEnabled(true) }
        await eventually { preparation != nil }
        initialEnable.cancel()
        preparation?.resume()
        do { try await initialEnable.value; Issue.record("Expected cancelled enable") }
        catch { #expect(error is CancellationError) }
        #expect(publications.isEmpty)
    }

    private func eventually(_ condition: () -> Bool) async {
        for _ in 0..<500 {
            if condition() { return }
            try? await Task.sleep(for: .milliseconds(2))
        }
        #expect(condition(), "Timed out waiting for microphone operation")
    }
}
