import Foundation
import Testing
@testable import AttentiveVoice

@Suite("Listening configuration")
struct AudioInputConfigurationTests {
    @Test(arguments: AttentiveAudioInputConfiguration.NoiseControl.allCases,
          AttentiveAudioInputConfiguration.ResponseSensitivity.allCases)
    func wireRoundTrip(noise: AttentiveAudioInputConfiguration.NoiseControl,
                       sensitivity: AttentiveAudioInputConfiguration.ResponseSensitivity) throws {
        let value = AttentiveAudioInputConfiguration(noiseControl: noise, responseSensitivity: sensitivity)
        let data = try JSONEncoder().encode(ListeningConfigurationMessage(value))
        let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(Set(object.keys) == ["version", "noiseControl", "responseSensitivity"])
        #expect(object["version"] as? Int == 1)
        #expect(try ListeningConfigurationMessage.acknowledgement(String(decoding: data, as: UTF8.self)) == value)
    }

    @Test(arguments: ["{}", "not json", #"{"version":2,"noiseControl":"standard","responseSensitivity":"balanced"}"#,
                      #"{"version":1,"noiseControl":"unknown","responseSensitivity":"balanced"}"#,
                      #"{"version":1,"noiseControl":"standard"}"#, String(repeating: " ", count: 4_097)])
    func rejectsInvalidAcknowledgement(_ reply: String) {
        #expect(throws: AttentiveAudioInputError.invalidAcknowledgement) {
            try ListeningConfigurationMessage.acknowledgement(reply)
        }
    }

    @Test
    func numericOverridesUseExactVersionTwoAndRoundTrip() throws {
        let value = AttentiveAudioInputConfiguration(noiseControl: .noisyEnvironment,
            noiseSuppressionDB: -12, minimumSpeechLevelDBFS: -50,
            speechConfirmationMilliseconds: 120, interruptionConfirmationMilliseconds: 240)
        try value.validate()
        let data = try JSONEncoder().encode(ListeningConfigurationMessage(value))
        let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(Set(object.keys) == ["version", "noiseControl", "responseSensitivity", "tuning"])
        #expect(object["version"] as? Int == 2)
        let tuning = try #require(object["tuning"] as? [String: Any])
        #expect(Set(tuning.keys) == ["noiseSuppressionDB", "minimumSpeechLevelDBFS",
                                   "speechConfirmationMilliseconds", "interruptionConfirmationMilliseconds"])
        #expect(try ListeningConfigurationMessage.acknowledgement(String(decoding: data, as: UTF8.self)) == value)
        let partial = AttentiveAudioInputConfiguration(minimumSpeechLevelDBFS: -45.5)
        let partialData = try JSONEncoder().encode(ListeningConfigurationMessage(partial))
        #expect(try ListeningConfigurationMessage.acknowledgement(String(decoding: partialData, as: UTF8.self)) == partial)
    }

    @Test(arguments: [
        AttentiveAudioInputConfiguration(noiseSuppressionDB: -12),
        .init(noiseControl: .noisyEnvironment, noiseSuppressionDB: -31),
        .init(noiseControl: .noisyEnvironment, noiseSuppressionDB: 1),
        .init(minimumSpeechLevelDBFS: -.infinity), .init(minimumSpeechLevelDBFS: .nan),
        .init(minimumSpeechLevelDBFS: -81), .init(minimumSpeechLevelDBFS: -9),
        .init(speechConfirmationMilliseconds: 0), .init(speechConfirmationMilliseconds: 61),
        .init(speechConfirmationMilliseconds: 520), .init(interruptionConfirmationMilliseconds: 60),
        .init(interruptionConfirmationMilliseconds: 101), .init(interruptionConfirmationMilliseconds: 1020)
    ])
    func invalidNumericValuesAreRejected(_ value: AttentiveAudioInputConfiguration) {
        #expect(throws: (any Error).self) { try value.validate() }
    }

    @Test(arguments: [
        #"{"version":2,"noiseControl":"standard","responseSensitivity":"balanced","tuning":{}}"#,
        #"{"version":2,"noiseControl":"standard","responseSensitivity":"balanced","tuning":{"pitch":2}}"#,
        #"{"version":2,"noiseControl":"standard","responseSensitivity":"balanced","tuning":{"minimumSpeechLevelDBFS":null}}"#,
        #"{"version":2,"noiseControl":"standard","responseSensitivity":"balanced","tuning":{"minimumSpeechLevelDBFS":true}}"#,
        #"{"version":2,"noiseControl":"standard","responseSensitivity":"balanced","tuning":{"noiseSuppressionDB":-12}}"#,
        #"{"version":2,"noiseControl":"standard","responseSensitivity":"balanced","tuning":{"speechConfirmationMilliseconds":121}}"#,
        #"{"version":1,"noiseControl":"standard","responseSensitivity":"balanced","tuning":{"minimumSpeechLevelDBFS":-50}}"#
    ])
    func invalidNumericAcknowledgementsAreRejected(_ reply: String) {
        #expect(throws: AttentiveAudioInputError.invalidAcknowledgement) {
            try ListeningConfigurationMessage.acknowledgement(reply)
        }
    }

    @Test @MainActor
    func numericRequestWaitsForSeparateCapabilityAndCanUpgradeLater() async throws {
        let rpc = ListeningRPCStub()
        let value = AttentiveAudioInputConfiguration(minimumSpeechLevelDBFS: -50)
        let negotiator = AudioInputNegotiator(configuration: value, timeout: .milliseconds(20), send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { negotiator.state.status == .unsupported }
        #expect(rpc.requests.isEmpty)
        #expect(negotiator.state.applied == nil)
        negotiator.updatePeer(identity: "agent", version: "1", numericVersion: "2")
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        #expect(negotiator.state.applied == value)
    }

    @Test @MainActor
    func unsupportedNumericUpdateRetainsAcknowledgedPresetAndCanReturnToVersionOne() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(timeout: .milliseconds(30), send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        for level in [-50.0, -40.0] {
            do {
                try await negotiator.configure(.init(minimumSpeechLevelDBFS: level))
                Issue.record("Expected unsupported numeric configuration")
            } catch { #expect(error as? AttentiveAudioInputError == .unsupported) }
            #expect(negotiator.state.applied == .standard)
            #expect(rpc.requests.count == 1)
        }
        let update = Task { try await negotiator.configure(.noisyEnvironment) }
        await eventually { rpc.requests.count == 2 }
        rpc.acknowledge(1)
        try await update.value
        #expect(negotiator.state.applied == .noisyEnvironment)
    }

    @Test @MainActor
    func numericRequestCannotBeAcknowledgedAsPreset() async throws {
        let rpc = ListeningRPCStub()
        let value = AttentiveAudioInputConfiguration(noiseControl: .noisyEnvironment, noiseSuppressionDB: -18)
        let negotiator = AudioInputNegotiator(configuration: value, send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1", numericVersion: "2")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0, payload: #"{"version":1,"noiseControl":"noisyEnvironment","responseSensitivity":"balanced"}"#)
        await eventually { negotiator.state.status == .failed }
        #expect(negotiator.state.applied == nil)
        #expect(negotiator.state.requested == value)
    }

    @Test @MainActor
    func invalidUpdateDoesNotSupersedePendingValidRequest() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(configuration: .noisyEnvironment, send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        do {
            try await negotiator.configure(.init(speechConfirmationMilliseconds: 1))
            Issue.record("Expected invalid configuration")
        } catch { #expect(error is AttentiveAudioInputError) }
        #expect(negotiator.state.requested == .noisyEnvironment)
        #expect(rpc.requests.count == 1)
        rpc.acknowledge(0)
        await eventually { negotiator.state.applied == .noisyEnvironment }
    }

    @Test @MainActor
    func invalidInitialSettingsDoNotRequestMicrophoneOrCreateTransport() async throws {
        var requestedPermission = false
        var createdTransport = false
        let call = AttentiveCall(provider: ListeningCredentials(), makeTransport: {
            createdTransport = true
            return ListeningCallTransport()
        }, microphonePermission: { requestedPermission = true; return true },
        audioInput: .init(minimumSpeechLevelDBFS: -90))
        do {
            try await call.start(.init(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "test@example.com"))
            Issue.record("Expected invalid initial configuration")
        } catch { #expect(error is AttentiveAudioInputError) }
        #expect(!requestedPermission)
        #expect(!createdTransport)
        #expect(call.state == .idle)
    }

    @Test @MainActor
    func invalidCallUpdatePreservesActiveConfigurationAndMute() async throws {
        let transport = ListeningCallTransport()
        let call = AttentiveCall(provider: ListeningCredentials(), makeTransport: { transport }, microphonePermission: { true })
        try await call.start(.init(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "test@example.com"), microphoneEnabled: false)
        let previous = call.audioInputState
        do {
            try await call.setAudioInputConfiguration(.init(noiseSuppressionDB: -12))
            Issue.record("Expected invalid update")
        } catch { #expect(error is AttentiveAudioInputError) }
        #expect(call.audioInputState == previous)
        #expect(call.audioInputConfiguration == .standard)
        #expect(call.state == .connected)
        #expect(!call.microphoneEnabled)
        #expect(transport.microphoneUpdates == 0)
        await call.end()
    }

    @Test @MainActor
    func lateAgentAndCapabilityDoNotClaimAppliedBeforeAcknowledgement() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(send: rpc.send)
        defer { negotiator.close() }
        try await negotiator.configure(.noisyEnvironment)
        #expect(negotiator.state.status == .idle)
        negotiator.setConnected(true)
        #expect(negotiator.state.status == .pending)
        #expect(negotiator.state.applied == nil)
        negotiator.updatePeer(identity: "agent-one", version: nil)
        #expect(rpc.requests.isEmpty)
        negotiator.updatePeer(identity: "agent-one", version: "1")
        await eventually { rpc.requests.count == 1 }
        #expect(rpc.requests[0].identity == "agent-one")
        #expect(negotiator.state.applied == nil)
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        #expect(negotiator.state.applied == .noisyEnvironment)
    }

    @Test @MainActor
    func legacyWorkerIsUnsupportedWithoutSendingRPCAndCanUpgradeLater() async {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(timeout: .milliseconds(20), send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "old-agent", version: nil)
        negotiator.setConnected(true)
        await eventually { negotiator.state.status == .unsupported }
        #expect(rpc.requests.isEmpty)
        #expect(negotiator.state.applied == nil)
        negotiator.updatePeer(identity: "old-agent", version: "1")
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
    }

    @Test @MainActor
    func serializesRapidUpdatesAndIgnoresStaleAcknowledgement() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        let first = Task { try await negotiator.configure(.noisyEnvironment) }
        await eventually { negotiator.state.requested == .noisyEnvironment }
        let latest = AttentiveAudioInputConfiguration(noiseControl: .noisyEnvironment, responseSensitivity: .strict)
        let second = Task { try await negotiator.configure(latest) }
        await eventually { negotiator.state.requested == latest }
        do { try await first.value; Issue.record("The old update should be superseded") }
        catch { #expect(error as? AttentiveAudioInputError == .superseded) }
        #expect(rpc.requests.count == 1)
        rpc.acknowledge(0)
        await eventually { rpc.requests.count == 2 }
        #expect(negotiator.state.applied == nil)
        #expect(try ListeningConfigurationMessage.acknowledgement(rpc.requests[1].payload) == latest)
        rpc.acknowledge(1)
        try await second.value
        #expect(negotiator.state.applied == latest)
    }

    @Test @MainActor
    func reconnectRequiresFreshAcknowledgement() async {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(configuration: .noisyEnvironment, send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "old-agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        negotiator.setConnected(false)
        negotiator.updatePeer(identity: "new-agent", version: "1")
        negotiator.setConnected(true)
        rpc.acknowledge(0)
        await eventually { rpc.requests.count == 2 }
        #expect(negotiator.state.applied == nil)
        #expect(rpc.requests[1].identity == "new-agent")
        rpc.acknowledge(1)
        await eventually { negotiator.state.status == .applied }
        negotiator.setConnected(false)
        #expect(negotiator.state.applied == nil)
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 3 }
        #expect(negotiator.state.status == .pending)
        rpc.acknowledge(2)
        await eventually { negotiator.state.status == .applied }
    }

    @Test @MainActor
    func rejectedUpdatePreservesLastAcknowledgedSettings() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        let update = Task { try await negotiator.configure(.noisyEnvironment) }
        await eventually { rpc.requests.count == 2 }
        rpc.reject(1, error: AttentiveAudioInputError.callerSpeaking)
        do { try await update.value; Issue.record("Expected busy worker rejection") }
        catch { #expect(error as? AttentiveAudioInputError == .callerSpeaking) }
        #expect(negotiator.state.status == .failed)
        #expect(negotiator.state.applied == .standard)
        #expect(negotiator.state.requested == .noisyEnvironment)
        #expect(negotiator.state.message?.contains("Pause speaking") == true)
    }

    @Test @MainActor
    func cancellationStopsWaitingButDoesNotMisrepresentAlreadySentUpdate() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        let update = Task { try await negotiator.configure(.noisyEnvironment) }
        await eventually { rpc.requests.count == 2 }
        update.cancel()
        do { try await update.value; Issue.record("Expected cancelled waiting caller") }
        catch { #expect(error is CancellationError) }
        #expect(negotiator.state.applied == .standard)
        rpc.acknowledge(1)
        await eventually { negotiator.state.applied == .noisyEnvironment }
    }

    @Test @MainActor
    func mismatchedAcknowledgementDoesNotClaimRequestedSettingsWereApplied() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        let update = Task { try await negotiator.configure(.noisyEnvironment) }
        await eventually { rpc.requests.count == 2 }
        rpc.acknowledge(1, payload: rpc.requests[0].payload)
        do { try await update.value; Issue.record("Expected mismatched acknowledgement rejection") }
        catch { #expect(error as? AttentiveAudioInputError == .invalidAcknowledgement) }
        #expect(negotiator.state.status == .failed)
        #expect(negotiator.state.applied == .standard)
        #expect(negotiator.state.requested == .noisyEnvironment)
    }

    @Test @MainActor
    func cancellingInsidePendingObserverDoesNotStrandAcceptedConfiguration() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        var update: Task<Void, Error>?
        negotiator.onChange = { state in
            if state.status == .pending, state.requested == .noisyEnvironment { update?.cancel() }
        }
        update = Task { try await negotiator.configure(.noisyEnvironment) }
        do { try await update?.value; Issue.record("Expected cancelled observer's waiter") }
        catch { #expect(error is CancellationError) }
        await eventually { rpc.requests.count == 2 }
        rpc.acknowledge(1)
        await eventually { negotiator.state.status == .applied }
        #expect(negotiator.state.applied == .noisyEnvironment)
    }

    @Test @MainActor
    func nonDefaultMicrophoneStartupWaitsForLateAgentAcknowledgement() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(configuration: .noisyEnvironment, send: rpc.send)
        defer { negotiator.close() }
        negotiator.setConnected(true)
        var microphonePublished = false
        let activation = Task {
            try await negotiator.prepareToEnableMicrophone()
            microphonePublished = true
        }
        await Task.yield()
        #expect(!microphonePublished)
        negotiator.updatePeer(identity: "agent", version: "1")
        await eventually { rpc.requests.count == 1 }
        #expect(!microphonePublished)
        rpc.acknowledge(0)
        try await activation.value
        #expect(microphonePublished)
        #expect(negotiator.state.applied == .noisyEnvironment)
    }

    @Test @MainActor
    func defaultMicrophoneStartupDoesNotWaitForAgent() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(send: rpc.send)
        defer { negotiator.close() }
        negotiator.setConnected(true)
        try await negotiator.prepareToEnableMicrophone()
        #expect(rpc.requests.isEmpty)
        #expect(negotiator.state.status == .pending)
    }

    @Test @MainActor
    func nonDefaultMicrophoneStartupOnLegacyWorkerIsBounded() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(configuration: .noisyEnvironment, timeout: .milliseconds(20), send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "legacy-agent", version: nil)
        negotiator.setConnected(true)
        let clock = ContinuousClock()
        let start = clock.now
        try await negotiator.prepareToEnableMicrophone()
        #expect(start.duration(to: clock.now) < .seconds(1))
        #expect(negotiator.state.status == .unsupported || negotiator.state.status == .failed)
        #expect(negotiator.state.applied == nil)
        #expect(rpc.requests.isEmpty)
    }

    @Test @MainActor
    func startupWaitsForLatestPreferenceWhenSelectionChangesDuringNegotiation() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(configuration: .noisyEnvironment, send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        var microphonePublished = false
        let activation = Task {
            try await negotiator.prepareToEnableMicrophone()
            microphonePublished = true
        }
        await eventually { rpc.requests.count == 1 }
        let latest = AttentiveAudioInputConfiguration(noiseControl: .noisyEnvironment, responseSensitivity: .strict)
        let update = Task { try await negotiator.configure(latest) }
        await eventually { negotiator.state.requested == latest }
        rpc.acknowledge(0)
        await eventually { rpc.requests.count == 2 }
        #expect(!microphonePublished)
        rpc.acknowledge(1)
        try await update.value
        try await activation.value
        #expect(microphonePublished)
        #expect(negotiator.state.applied == latest)
    }

    @Test @MainActor
    func unavailableProcessingStillAllowsMicrophoneStartupWithoutClaimingApplication() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(configuration: .noisyEnvironment, send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        let activation = Task { try await negotiator.prepareToEnableMicrophone() }
        await eventually { rpc.requests.count == 1 }
        rpc.reject(0, error: AttentiveAudioInputError.processingUnavailable)
        try await activation.value
        #expect(negotiator.state.status == .failed)
        #expect(negotiator.state.applied == nil)
    }

    @Test @MainActor
    func cancellingStartupNeverEnablesMicrophoneAfterLateAcknowledgement() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(configuration: .noisyEnvironment, send: rpc.send)
        defer { negotiator.close() }
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        var microphonePublished = false
        let activation = Task {
            try await negotiator.prepareToEnableMicrophone()
            microphonePublished = true
        }
        await eventually { rpc.requests.count == 1 }
        activation.cancel()
        do { try await activation.value; Issue.record("Expected cancelled activation") }
        catch { #expect(error is CancellationError) }
        rpc.acknowledge(0)
        await eventually { negotiator.state.status == .applied }
        #expect(!microphonePublished)
    }

    @Test @MainActor
    func timeoutAndDisconnectDoNotInventSuccessfulApplication() async throws {
        let rpc = ListeningRPCStub()
        let negotiator = AudioInputNegotiator(timeout: .milliseconds(20), send: rpc.send)
        negotiator.updatePeer(identity: "agent", version: "1")
        negotiator.setConnected(true)
        await eventually { rpc.requests.count == 1 }
        let update = Task { try await negotiator.configure(.noisyEnvironment) }
        do { try await update.value; Issue.record("Expected bounded timeout") }
        catch { #expect(error as? AttentiveAudioInputError == .timedOut) }
        #expect(negotiator.state.status == .failed)
        #expect(negotiator.state.applied == nil)
        negotiator.close()
        rpc.acknowledge(0)
        await Task.yield()
        #expect(negotiator.state.status == .idle)
        #expect(negotiator.state.applied == nil)
        #expect(rpc.requests.count == 1)
    }

    @Test @MainActor
    func callKeepsMuteAndSessionOnUnsupportedSettingsAndRetainsNextCallPreference() async throws {
        let transport = ListeningCallTransport()
        let call = AttentiveCall(provider: ListeningCredentials(), makeTransport: { transport },
                                 microphonePermission: { true })
        try await call.setAudioInputConfiguration(.noisyEnvironment)
        #expect(call.audioInputState.status == .idle)
        #expect(call.audioInputState.applied == nil)
        let request = CallRequest(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "test@example.com")
        try await call.start(request, microphoneEnabled: false)
        #expect(call.audioInputState.applied == .noisyEnvironment)
        transport.error = .unsupported
        let updated = AttentiveAudioInputConfiguration(responseSensitivity: .strict)
        do { try await call.setAudioInputConfiguration(updated); Issue.record("Expected unsupported settings") }
        catch { #expect(error as? AttentiveAudioInputError == .unsupported) }
        #expect(call.state == .connected)
        #expect(call.lastError == nil)
        #expect(call.microphoneEnabled == false)
        #expect(transport.microphoneUpdates == 0)
        #expect(call.audioInputState.status == .unsupported)
        #expect(call.audioInputState.applied == .noisyEnvironment)
        await call.end()
        #expect(call.audioInputConfiguration == updated)
        #expect(call.audioInputState.applied == nil)
        transport.error = nil
        try await call.start(request, microphoneEnabled: false)
        #expect(call.audioInputState.applied == updated)
        await call.end()
    }

    @Test @MainActor
    func cancellingFromCallPendingEventLeavesSettingsRetryable() async throws {
        let transport = ListeningCallTransport()
        let call = AttentiveCall(provider: ListeningCredentials(), makeTransport: { transport },
                                 microphonePermission: { true })
        let request = CallRequest(businessSlug: "test", agentPublicId: "agt_test", endUserContact: "test@example.com")
        try await call.start(request, microphoneEnabled: false)
        var update: Task<Void, Error>?
        call.onEvent = { event in
            if case .audioInputChanged(let state) = event, state.status == .pending { update?.cancel() }
        }
        update = Task { try await call.setAudioInputConfiguration(.noisyEnvironment) }
        do { try await update?.value; Issue.record("Expected cancelled configuration waiting") }
        catch { #expect(error is CancellationError) }
        #expect(call.audioInputState.status != .pending)
        #expect(call.state == .connected)
        #expect(call.lastError == nil)
        call.onEvent = nil
        try await call.setAudioInputConfiguration(.noisyEnvironment)
        #expect(call.audioInputState.applied == .noisyEnvironment)
        await call.end()
    }

    @MainActor
    private func eventually(_ condition: () -> Bool) async {
        for _ in 0..<500 {
            if condition() { return }
            try? await Task.sleep(for: .milliseconds(2))
        }
        #expect(condition(), "Timed out waiting for asynchronous listening state")
    }
}

@MainActor
private final class ListeningRPCStub {
    struct Request {
        let identity: String
        let payload: String
        let continuation: CheckedContinuation<String, Error>
    }
    var requests: [Request] = []
    func send(identity: String, payload: String) async throws -> String {
        try await withCheckedThrowingContinuation { requests.append(.init(identity: identity, payload: payload, continuation: $0)) }
    }
    func acknowledge(_ index: Int, payload: String? = nil) {
        requests[index].continuation.resume(returning: payload ?? requests[index].payload)
    }
    func reject(_ index: Int, error: Error) { requests[index].continuation.resume(throwing: error) }
}

private struct ListeningCredentials: CallCredentialProvider {
    func fetch(for request: CallRequest) async throws -> CallCredentials {
        .init(serverUrl: URL(string: "wss://example.com")!, roomName: "listening-test", participantToken: "test-token")
    }
}

@MainActor
private final class ListeningCallTransport: CallTransport {
    var onEvent: ((CallEvent) -> Void)?
    var error: AttentiveAudioInputError?
    var microphoneEnabled: Bool? = false
    var microphoneUpdates = 0
    private var connected = false
    private var requested = AttentiveAudioInputConfiguration.standard

    func connect(_ credentials: CallCredentials, microphoneEnabled: Bool) async throws {
        connected = true
        self.microphoneEnabled = microphoneEnabled
        onEvent?(.agentStateChanged(.listening))
        onEvent?(.audioInputChanged(.init(status: .applied, requested: requested, applied: requested)))
    }
    func disconnect() async { connected = false }
    func setMicrophone(enabled: Bool) async throws { microphoneUpdates += 1; microphoneEnabled = enabled }
    func sendText(_ text: String) async throws -> String { "test" }
    func setAudioInputConfiguration(_ configuration: AttentiveAudioInputConfiguration) async throws {
        try Task.checkCancellation()
        if let error { throw error }
        requested = configuration
        if connected { onEvent?(.audioInputChanged(.init(status: .applied, requested: requested, applied: requested))) }
    }
}
