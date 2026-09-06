import AVFoundation
import Combine
import XCTest
@testable import AttentiveVoice

final class AudioLevelTests: XCTestCase {
    func testSilenceQuietSpeechAndLoudSpeechHaveDifferentEnergy() throws {
        func energy(_ amplitude: Float) throws -> Float {
            let buffer = try floatBuffer([[amplitude, -amplitude, amplitude, -amplitude]])
            var level = AudioBufferLevel()
            level.include(buffer)
            return level.energy
        }
        XCTAssertEqual(try energy(0), 0)
        XCTAssertEqual(try energy(0.0001), 0)
        let quiet = try energy(0.015)
        let medium = try energy(0.08)
        let loud = try energy(0.2)
        XCTAssertGreaterThan(quiet, 0)
        XCTAssertGreaterThan(medium, quiet)
        XCTAssertGreaterThan(loud, medium)
        XCTAssertEqual(try energy(1), 1.65, accuracy: 0.0001)
    }

    func testStereoFloatBuffersUseStrideAndRemainUnchanged() throws {
        let channels: [[Float]] = [[0.1, -0.2, 0.3, -0.4], [0.4, -0.3, 0.2, -0.1]]
        for interleaved in [false, true] {
            let buffer = try floatBuffer(channels, interleaved: interleaved)
            var level = AudioBufferLevel()
            level.include(buffer)
            XCTAssertEqual(level.sampleCount, 8)
            XCTAssertEqual(level.sumOfSquares, 0.6, accuracy: 0.0001)
            XCTAssertEqual(level.peak, 0.4, accuracy: 0.0001)
            let data = try XCTUnwrap(buffer.floatChannelData)
            for channel in channels.indices {
                for frame in channels[channel].indices {
                    XCTAssertEqual(data[channel][frame * buffer.stride], channels[channel][frame])
                }
            }
        }
    }

    func testIntegerPCMIsNormalizedAndWindowsAccumulate() throws {
        for interleaved in [false, true] {
            for format in [AVAudioCommonFormat.pcmFormatInt16, .pcmFormatInt32] {
                let audioFormat = try XCTUnwrap(AVAudioFormat(commonFormat: format, sampleRate: 48000,
                                                             channels: 2, interleaved: interleaved))
                let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: audioFormat, frameCapacity: 2))
                buffer.frameLength = 2
                for channel in 0..<2 {
                    for frame in 0..<2 {
                        if format == .pcmFormatInt16 {
                            buffer.int16ChannelData![channel][frame * buffer.stride] = channel == 0 ? 16384 : -16384
                        } else {
                            buffer.int32ChannelData![channel][frame * buffer.stride] = channel == 0 ? 1_073_741_824 : -1_073_741_824
                        }
                    }
                }
                var level = AudioBufferLevel()
                level.include(buffer)
                level.include(buffer)
                XCTAssertEqual(level.sampleCount, 8)
                XCTAssertEqual(level.sumOfSquares, 2, accuracy: 0.0001)
                XCTAssertEqual(level.peak, 0.5)
            }
        }
    }

    func testInvalidSamplesCannotProduceInvalidEnergy() throws {
        var level = AudioBufferLevel()
        XCTAssertEqual(level.energy, 0)
        level.include(try floatBuffer([[.nan, .infinity, -.infinity, 0]]))
        XCTAssertEqual(level.energy, 0)
        level.include(try floatBuffer([[2, -2]]))
        XCTAssertEqual(level.peak, 1)
        XCTAssertEqual(level.energy, 1.65, accuracy: 0.0001)
    }

    @MainActor
    func testUpdatesDoNotInsertFalseSilenceAndMissingSamplesReturnToRest() async throws {
        let meter = AgentAudioLevel()
        var samples: [Float] = []
        let subscription = meter.$energy.sink { samples.append($0) }
        defer { subscription.cancel() }
        meter.update(0.2)
        meter.update(0.6)
        XCTAssertEqual(samples, [0, 0.2, 0.6])
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertEqual(meter.energy, 0)
        XCTAssertEqual(samples, [0, 0.2, 0.6, 0])
    }

    @MainActor
    func testNewSampleRefreshesSilenceDeadlineAndResetClearsLevel() async throws {
        let meter = AgentAudioLevel()
        meter.update(0.2)
        try await Task.sleep(for: .milliseconds(100))
        meter.update(0.8)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(meter.energy, 0.8)
        meter.reset()
        XCTAssertEqual(meter.energy, 0)
        meter.update(.nan)
        XCTAssertEqual(meter.energy, 0)
        meter.update(9)
        XCTAssertEqual(meter.energy, 1.65)
        meter.reset()
    }

    private func floatBuffer(_ channels: [[Float]], interleaved: Bool = false) throws -> AVAudioPCMBuffer {
        let format = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000,
                                                channels: AVAudioChannelCount(channels.count), interleaved: interleaved))
        let frames = channels[0].count
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)))
        buffer.frameLength = AVAudioFrameCount(frames)
        let data = try XCTUnwrap(buffer.floatChannelData)
        for channel in channels.indices {
            for frame in 0..<frames { data[channel][frame * buffer.stride] = channels[channel][frame] }
        }
        return buffer
    }
}
