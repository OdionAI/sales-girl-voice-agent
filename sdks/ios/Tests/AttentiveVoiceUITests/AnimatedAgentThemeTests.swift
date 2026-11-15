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
}
