import Foundation

// Dashboard-owned theme keys are optional; unknown values retain the existing avatar.
struct AnimatedAgentTheme: Equatable {
    enum Texture: String { case grain, smooth }

    let rgb: UInt32
    let texture: Texture

    init?(key: String?) {
        guard let key else { return nil }
        let parts = key.lowercased().split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0] == "animated", parts[1].count == 7,
              parts[1].first == "#", let texture = Texture(rawValue: String(parts[2])) else { return nil }
        let hex = parts[1].dropFirst()
        guard hex.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
              let rgb = UInt32(hex, radix: 16) else { return nil }
        self.rgb = rgb
        self.texture = texture
    }

    static func speechIntensity(speaking: Bool, energy: Float, reduceMotion: Bool, sceneActive: Bool) -> Double {
        guard speaking, !reduceMotion, sceneActive, energy.isFinite, energy > 0.01 else { return 0 }
        return Double(min(1, max(0, energy)))
    }
}
