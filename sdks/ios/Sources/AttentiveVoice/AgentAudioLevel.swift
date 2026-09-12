import AVFoundation
import Combine
import Foundation

/// Read-only agent output energy for visualizations. Observe this object separately
/// from the call so audio samples do not invalidate the rest of the interface.
@MainActor
public final class AgentAudioLevel: ObservableObject {
    /// Speech energy in 0...1.65. This is not playback gain or microphone volume.
    @Published public private(set) var energy: Float = 0
    private var staleSample: Task<Void, Never>?

    func update(_ value: Float) {
        staleSample?.cancel()
        staleSample = nil
        let bounded = value.isFinite ? min(1.65, max(0, value)) : 0
        if energy != bounded { energy = bounded }
        guard bounded > 0 else { return }
        staleSample = Task { [weak self] in
            do { try await Task.sleep(for: .milliseconds(160)) } catch { return }
            self?.reset()
        }
    }

    func reset() {
        staleSample?.cancel()
        staleSample = nil
        if energy != 0 { energy = 0 }
    }
}

/// Scalar statistics only: never retains, rewrites, converts or plays PCM buffers.
struct AudioBufferLevel {
    var sumOfSquares: Double = 0
    var sampleCount = 0
    var peak: Float = 0

    var energy: Float {
        guard sampleCount > 0 else { return 0 }
        let rms = Float(sqrt(sumOfSquares / Double(sampleCount)))
        guard rms >= 0.001 else { return 0 }
        // Match the web's bounded, boosted energy range; RMS/peak replace its FFT bands.
        return min(1.65, pow(rms * 5.5 + peak * 1.4, 0.88))
    }

    mutating func include(_ buffer: AVAudioPCMBuffer) {
        let channels = Int(buffer.format.channelCount)
        let frames = Int(buffer.frameLength)
        let stride = buffer.stride
        if let data = buffer.floatChannelData {
            for channel in 0..<channels {
                for frame in 0..<frames { include(data[channel][frame * stride]) }
            }
        } else if let data = buffer.int16ChannelData {
            for channel in 0..<channels {
                for frame in 0..<frames { include(Float(data[channel][frame * stride]) / 32768) }
            }
        } else if let data = buffer.int32ChannelData {
            for channel in 0..<channels {
                for frame in 0..<frames { include(Float(data[channel][frame * stride]) / 2_147_483_648) }
            }
        }
    }

    private mutating func include(_ value: Float) {
        let amplitude = value.isFinite ? min(1, abs(value)) : 0
        sumOfSquares += Double(amplitude * amplitude)
        sampleCount += 1
        peak = max(peak, amplitude)
    }
}
