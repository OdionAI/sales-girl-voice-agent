import AttentiveVoice
import Combine
import Foundation

/// View-owned presentation state; uses the same public core API as a customer's own UI.
@MainActor
final class CallerControls: ObservableObject {
    let call: AttentiveCall
    let enrollment: VoiceEnrollment?
    @Published var errorMessage: String?
    @Published private(set) var errorTitle = "Call unavailable"
    @Published private(set) var receivedAudio = false
    @Published private(set) var sending = false
    @Published private(set) var enrollmentBusy = false
    private var subscriptions: Set<AnyCancellable> = []
    private var visible = true

    init(call: AttentiveCall, enrollment: VoiceEnrollment?) {
        self.call = call
        self.enrollment = enrollment
        // Observe published state without taking over the host's onEvent callback.
        call.$lastError.sink { [weak self] error in
            self?.presentError(error)
        }.store(in: &subscriptions)
        call.$state.removeDuplicates().sink { [weak self] state in
            if state == .connecting { self?.receivedAudio = false }
        }.store(in: &subscriptions)
        call.agentAudioLevel.$energy.map { $0 > 0 }.removeDuplicates().sink { [weak self] audible in
            guard let self, audible, !self.receivedAudio else { return }
            self.receivedAudio = true
        }.store(in: &subscriptions)
        enrollment?.$stage.sink { [weak self] stage in
            self?.enrollmentBusy = stage == .recording || stage == .saving
        }.store(in: &subscriptions)
    }

    var active: Bool { [.connecting, .connected, .reconnecting, .ending].contains(call.state) }

    func start(_ request: CallRequest, microphoneEnabled: Bool) async {
        guard visible, !active, enrollment?.isBusy != true else { return }
        presentError(nil)
        do { try await call.start(request, microphoneEnabled: microphoneEnabled) }
        catch is CancellationError {}
        catch { presentError(error) }
    }

    func end() async { await call.end() }

    func toggleMicrophone() async {
        do { try await call.setMicrophone(enabled: !call.microphoneEnabled) }
        catch { presentError(error) }
    }

    func send(_ text: String) async -> Bool {
        guard !sending else { return false }
        sending = true
        defer { sending = false }
        do { try await call.sendText(text); return true }
        catch { presentError(error); return false }
    }

    private func presentError(_ error: Error?) {
        switch error as? CallError {
        case .insufficientCredits: errorTitle = "Call credit needed"
        case .httpStatus(402): errorTitle = "Payment required"
        default: errorTitle = "Call unavailable"
        }
        errorMessage = error?.localizedDescription
    }

    func appear() { visible = true }

    func disappear(endsCall: Bool) async {
        visible = false
        enrollment?.cancel()
        if endsCall, active { await call.end() }
    }
}
