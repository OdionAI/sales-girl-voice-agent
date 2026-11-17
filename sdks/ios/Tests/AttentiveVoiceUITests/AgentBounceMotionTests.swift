import XCTest
@testable import AttentiveVoiceUI

final class AgentBounceMotionTests: XCTestCase {
    func testSpeakingAudioDrivesScaleForEveryTheme() {
        // Theme selection is deliberately absent: static and animated avatars share motion.
        XCTAssertEqual(AgentBounceMotion.scale(energy: 0.8, speaking: true, connected: true,
                                              sceneActive: true, reduceMotion: false), 1.2, accuracy: 0.0001)
        XCTAssertEqual(AgentBounceMotion.scale(energy: 100, speaking: true, connected: true,
                                              sceneActive: true, reduceMotion: false), 1.4125, accuracy: 0.0001)
    }

    func testIdleBackgroundAndReducedMotionRemainStill() {
        XCTAssertEqual(AgentBounceMotion.scale(energy: 0.8, speaking: false, connected: true,
                                              sceneActive: true, reduceMotion: false), 1)
        XCTAssertEqual(AgentBounceMotion.scale(energy: 0.8, speaking: true, connected: false,
                                              sceneActive: true, reduceMotion: false), 1)
        XCTAssertEqual(AgentBounceMotion.scale(energy: 0.8, speaking: true, connected: true,
                                              sceneActive: false, reduceMotion: false), 1)
        XCTAssertEqual(AgentBounceMotion.scale(energy: 0.8, speaking: true, connected: true,
                                              sceneActive: true, reduceMotion: true), 1)
        XCTAssertEqual(AgentBounceMotion.scale(energy: .nan, speaking: true, connected: true,
                                              sceneActive: true, reduceMotion: false), 1)
    }

    func testFastAttackAndGentleRelease() {
        let attack = AgentBounceMotion.smoothedEnergy(current: 0, target: 1)
        XCTAssertGreaterThan(attack, 0.9)
        XCTAssertLessThan(attack, 1)
        let release = AgentBounceMotion.smoothedEnergy(current: attack, target: 0)
        XCTAssertGreaterThan(release, 0.4)
        XCTAssertLessThan(release, attack)
        XCTAssertEqual(AgentBounceMotion.smoothedEnergy(current: 0, target: .nan), 0)
    }
}
