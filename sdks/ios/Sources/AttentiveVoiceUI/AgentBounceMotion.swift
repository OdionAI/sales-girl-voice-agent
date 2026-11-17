import Foundation

/// Speech-driven avatar motion shared by every dashboard theme.
/// The asymmetric envelope follows the web caller's fast attack and soft release.
struct AgentBounceMotion {
    static func smoothedEnergy(current: Double, target: Float) -> Double {
        let previous = current.isFinite ? min(1.65, max(0, current)) : 0
        let nextTarget = target.isFinite ? min(1.65, max(0, Double(target))) : 0
        // Web uses 0.76 attack / 0.26 release per 60 Hz frame. The iOS meter
        // publishes every 40 ms, so apply the equivalent 2.4-frame response.
        let frameRetention = nextTarget > previous ? 0.24 : 0.74
        let next = nextTarget + (previous - nextTarget) * pow(frameRetention, 2.4)
        return next < 0.002 ? 0 : next
    }

    static func scale(energy: Double, speaking: Bool, connected: Bool,
                      sceneActive: Bool, reduceMotion: Bool) -> Double {
        guard speaking, connected, sceneActive, !reduceMotion, energy.isFinite,
              energy > 0.01 else { return 1 }
        return 1 + min(1.65, max(0, energy)) * 0.25
    }
}
