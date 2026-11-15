import Foundation
import XCTest
@testable import AttentiveVoiceUI

final class AnimatedAgentThemeTests: XCTestCase {
    func testValidThemeKeysPreserveArbitraryRGBAndTexture() throws {
        let grain = try XCTUnwrap(AnimatedAgentTheme(key: "animated:#2F6FED:grain"))
        XCTAssertEqual(grain.rgb, 0x2F6FED)
        XCTAssertEqual(grain.texture, .grain)

        let smooth = try XCTUnwrap(AnimatedAgentTheme(key: "AnImAtEd:#aB09fF:SmOoTh"))
        XCTAssertEqual(smooth.rgb, 0xAB09FF)
        XCTAssertEqual(smooth.texture, .smooth)

        XCTAssertEqual(AnimatedAgentTheme(key: "animated:#000000:smooth")?.rgb, 0)
        XCTAssertEqual(AnimatedAgentTheme(key: "animated:#ffffff:grain")?.rgb, 0xFFFFFF)
    }

    func testInvalidAndExistingThemeKeysDoNotMatchAnimatedTheme() {
        let values: [String?] = [nil, "", "default", "theme_2", "theme_3", "color:#112233",
            "animated:#fff:grain", "animated:#1234567:grain", "animated:123456:grain",
            "animated:#12345g:grain", "animated:#123456:other", "animated:#123456:",
            "animated:#123456:grain:extra", " animated:#123456:grain", "animated:#123456:grain\n"]
        for value in values {
            XCTAssertNil(AnimatedAgentTheme(key: value), "Unexpected animated key: \(String(describing: value))")
        }
    }

    func testPaletteMatchesDashboardReferenceForDefaultAndArbitraryColors() throws {
        // These expected values come from the shared dashboard appearance contract,
        // including its special original-blue palette and per-channel rounding.
        let cases: [(String, [UInt32])] = [
            ("#2F6FED", [0x0263D9, 0x65A6EB, 0x9CC0F0]),
            ("#000000", [0x000000, 0x575757, 0xA6A6A6]),
            ("#FFFFFF", [0xCCCCCC, 0xFFFFFF, 0xFFFFFF]),
            ("#AB09FF", [0x8907CC, 0xC85DFF, 0xE2A9FF]),
            ("#0081FE", [0x0067CB, 0x57ACFE, 0xA6D3FF]),
            ("#123456", [0x0E2A45, 0x63798F, 0xACB8C4]),
        ]
        for (color, expected) in cases {
            for texture in ["grain", "smooth"] {
                let theme = try XCTUnwrap(AnimatedAgentTheme(key: "animated:\(color):\(texture)"))
                XCTAssertEqual(theme.palette, expected, "Palette mismatch for \(color) / \(texture)")
            }
        }
    }

    func testSpeechMotionRequiresSpeakingActiveSceneAndMotionAllowed() {
        let audible = AnimatedAgentTheme.speechIntensity(speaking: true, energy: 0.7,
                                                        reduceMotion: false, sceneActive: true)
        XCTAssertGreaterThan(audible, 0)
        XCTAssertLessThanOrEqual(audible, 1)
        XCTAssertEqual(AnimatedAgentTheme.speechIntensity(speaking: false, energy: 0.7,
                                                         reduceMotion: false, sceneActive: true), 0)
        XCTAssertEqual(AnimatedAgentTheme.speechIntensity(speaking: true, energy: 0.7,
                                                         reduceMotion: true, sceneActive: true), 0)
        XCTAssertEqual(AnimatedAgentTheme.speechIntensity(speaking: true, energy: 0.7,
                                                         reduceMotion: false, sceneActive: false), 0)
    }

    func testSpeechIntensityClampsAndRejectsInvalidSamples() {
        let invalid: [Float] = [0, -1, .nan, .infinity, -.infinity]
        for value in invalid {
            XCTAssertEqual(AnimatedAgentTheme.speechIntensity(speaking: true, energy: value,
                                                             reduceMotion: false, sceneActive: true), 0)
        }
        let quiet = AnimatedAgentTheme.speechIntensity(speaking: true, energy: 0.2,
                                                      reduceMotion: false, sceneActive: true)
        let loud = AnimatedAgentTheme.speechIntensity(speaking: true, energy: 0.8,
                                                     reduceMotion: false, sceneActive: true)
        XCTAssertGreaterThan(loud, quiet)
        XCTAssertEqual(AnimatedAgentTheme.speechIntensity(speaking: true, energy: 100,
                                                         reduceMotion: false, sceneActive: true), 1)
    }

    func testTalkingThresholdMatchesDashboardAndStopsOnInterruption() {
        func intensity(_ speaking: Bool, _ energy: Float) -> Double {
            AnimatedAgentTheme.speechIntensity(speaking: speaking, energy: energy,
                                               reduceMotion: false, sceneActive: true)
        }
        XCTAssertEqual(intensity(true, 0.01), 0)
        XCTAssertGreaterThan(intensity(true, 0.011), 0)
        XCTAssertGreaterThan(intensity(true, 0.7), 0)
        XCTAssertEqual(intensity(false, 0.7), 0, "Interrupted speech must stop reactive motion immediately")
        XCTAssertEqual(intensity(true, 0), 0, "Silence must not be treated as speech")
    }

    func testShaderReceivesPaletteTextureAndOnlyAudibleAssistantSpeech() throws {
        let theme = try XCTUnwrap(AnimatedAgentTheme(key: "animated:#ab09ff:smooth"))
        let state = ReferenceShaderState(theme: theme, speaking: true, connected: true, energy: 0.65,
                                         reduceMotion: false, sceneActive: true, visible: true)
        XCTAssertEqual(state.colors, ["#8907cc", "#c85dff", "#e2a9ff"])
        XCTAssertEqual(state.texture, "smooth")
        XCTAssertEqual(state.mode, "talking")
        XCTAssertEqual(state.intensity, 0.65, accuracy: 0.0001)
        XCTAssertFalse(state.paused)

        let encoded = try JSONEncoder().encode(state)
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertEqual(Set(payload.keys), ["colors", "texture", "mode", "intensity", "paused"])
        XCTAssertEqual(payload["mode"] as? String, "talking")
        XCTAssertEqual(payload["texture"] as? String, "smooth")
    }

    func testShaderStopsTalkingOnSilenceInterruptionDisconnectAndPause() throws {
        let theme = try XCTUnwrap(AnimatedAgentTheme(key: "animated:#2f6fed:grain"))
        func state(speaking: Bool = true, connected: Bool = true, energy: Float = 0.7,
                   reduceMotion: Bool = false, sceneActive: Bool = true, visible: Bool = true) -> ReferenceShaderState {
            ReferenceShaderState(theme: theme, speaking: speaking, connected: connected, energy: energy,
                                 reduceMotion: reduceMotion, sceneActive: sceneActive, visible: visible)
        }
        for result in [state(energy: 0), state(speaking: false), state(connected: false)] {
            XCTAssertEqual(result.mode, "listening")
            XCTAssertEqual(result.intensity, 0)
            XCTAssertFalse(result.paused, "Listening animation can continue while the UI is active")
        }
        for result in [state(reduceMotion: true), state(sceneActive: false), state(visible: false)] {
            XCTAssertEqual(result.mode, "listening")
            XCTAssertEqual(result.intensity, 0)
            XCTAssertTrue(result.paused)
        }
    }
}
