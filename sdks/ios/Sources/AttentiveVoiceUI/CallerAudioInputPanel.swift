#if os(iOS)
import AttentiveVoice
import SwiftUI

@available(iOS 17, *)
struct CallerAudioInputPanel: View {
    @ObservedObject var controls: CallerControls
    @ObservedObject var call: AttentiveCall
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .headline) private var titleSize = 16.0
    @ScaledMetric(relativeTo: .subheadline) private var labelSize = 14.0
    @ScaledMetric(relativeTo: .footnote) private var bodySize = 13.0
    @ScaledMetric(relativeTo: .caption) private var detailSize = 12.0

    private var isBusy: Bool {
        controls.updatingAudioInput || controls.starting || controls.enrollmentBusy
            || [.connecting, .reconnecting, .ending].contains(call.state)
            || call.audioInputState.status == .pending
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Listening settings").font(.system(size: titleSize, weight: .semibold))
                    Text("Adjust how the agent hears and responds to you.")
                        .font(.system(size: bodySize)).foregroundStyle(CallerTheme.muted)
                }

                VStack(alignment: .leading, spacing: 10) {
                    Text("Noise control").font(.system(size: labelSize, weight: .medium))
                    if dynamicTypeSize.isAccessibilitySize {
                        noisePicker.pickerStyle(.menu)
                    } else {
                        noisePicker.pickerStyle(.segmented)
                    }
                    Text("Use Noisy environment around traffic, music, or other background sounds. Nearby voices may still be heard.")
                        .font(.system(size: detailSize)).foregroundStyle(CallerTheme.muted)
                }
                .disabled(isBusy)

                VStack(alignment: .leading, spacing: 10) {
                    Text("Response sensitivity").font(.system(size: labelSize, weight: .medium))
                    if dynamicTypeSize.isAccessibilitySize {
                        sensitivityPicker.pickerStyle(.menu)
                    } else {
                        sensitivityPicker.pickerStyle(.segmented)
                    }
                    Text(sensitivityDescription)
                        .font(.system(size: detailSize)).foregroundStyle(CallerTheme.muted)
                        .accessibilityIdentifier("responseSensitivityDescription")
                }
                .disabled(isBusy)

                Rectangle().fill(CallerTheme.divider).frame(height: 1)
                stateDescription
            }
            .padding(18)
        }
        .scrollBounceBehavior(.basedOnSize)
    }

    private var noisePicker: some View {
        Picker("Noise control", selection: noiseControl) {
            Text("Standard").tag(AttentiveAudioInputConfiguration.NoiseControl.standard)
            Text("Noisy environment").tag(AttentiveAudioInputConfiguration.NoiseControl.noisyEnvironment)
        }
        .accessibilityIdentifier("noiseControl")
    }

    private var sensitivityPicker: some View {
        Picker("Response sensitivity", selection: responseSensitivity) {
            Text("Responsive").tag(AttentiveAudioInputConfiguration.ResponseSensitivity.responsive)
            Text("Balanced").tag(AttentiveAudioInputConfiguration.ResponseSensitivity.balanced)
            Text("Strict").tag(AttentiveAudioInputConfiguration.ResponseSensitivity.strict)
        }
        .accessibilityIdentifier("responseSensitivity")
    }

    private var noiseControl: Binding<AttentiveAudioInputConfiguration.NoiseControl> {
        Binding(get: { call.audioInputConfiguration.noiseControl }, set: { value in
            var configuration = call.audioInputConfiguration
            configuration.noiseControl = value
            Task { await controls.setAudioInputConfiguration(configuration) }
        })
    }

    private var responseSensitivity: Binding<AttentiveAudioInputConfiguration.ResponseSensitivity> {
        Binding(get: { call.audioInputConfiguration.responseSensitivity }, set: { value in
            var configuration = call.audioInputConfiguration
            configuration.responseSensitivity = value
            Task { await controls.setAudioInputConfiguration(configuration) }
        })
    }

    private var sensitivityDescription: String {
        switch call.audioInputConfiguration.responseSensitivity {
        case .responsive: return "Responds to shorter, quieter speech. May react more often to background sounds."
        case .balanced: return "Balances quick replies with avoiding accidental responses."
        case .strict: return "Requires clearer speech and waits longer before interrupting the agent. Quiet words may be missed."
        }
    }

    private var stateDescription: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 8) {
                if call.audioInputState.status == .pending || controls.updatingAudioInput {
                    ProgressView().controlSize(.small).accessibilityHidden(true)
                }
                Text(statusText).font(.system(size: detailSize)).foregroundStyle(statusColor)
                    .accessibilityIdentifier("audioInputStatus")
            }
            if let applied = call.audioInputState.applied, applied != call.audioInputConfiguration {
                Text("Last confirmed: \(applied.noiseControl == .standard ? "Standard" : "Noisy environment") · \(sensitivityLabel(applied.responseSensitivity))")
                    .font(.system(size: detailSize)).foregroundStyle(CallerTheme.muted)
                    .accessibilityIdentifier("audioInputEffectiveSettings")
            }
            if controls.audioInputError != nil || call.audioInputState.status == .failed {
                Button("Try again", systemImage: "arrow.clockwise") {
                    Task { await controls.setAudioInputConfiguration(call.audioInputConfiguration) }
                }
                .font(.system(size: bodySize, weight: .medium)).frame(minHeight: 44)
                .buttonStyle(.plain).disabled(isBusy)
                .accessibilityIdentifier("retryAudioInput")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusText: String {
        if let error = controls.audioInputError { return error }
        switch call.audioInputState.status {
        case .idle: return "Saved for your next call."
        case .pending: return "Applying to this call…"
        case .applied: return "Applied to this call."
        case .unsupported: return "Listening settings aren't supported by this call."
        case .failed: return call.audioInputState.message ?? "Couldn't confirm these settings. Try again."
        }
    }

    private var statusColor: Color {
        controls.audioInputError != nil || call.audioInputState.status == .failed ? CallerTheme.red : CallerTheme.muted
    }

    private func sensitivityLabel(_ sensitivity: AttentiveAudioInputConfiguration.ResponseSensitivity) -> String {
        switch sensitivity {
        case .responsive: return "Responsive"
        case .balanced: return "Balanced"
        case .strict: return "Strict"
        }
    }
}
#endif
