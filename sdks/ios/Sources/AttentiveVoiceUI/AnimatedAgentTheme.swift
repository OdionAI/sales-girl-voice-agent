import Foundation

// Dashboard-owned theme keys are optional; unknown values retain the existing avatar.
struct AnimatedAgentTheme: Equatable {
    enum Texture: String { case grain, smooth }

    let rgb: UInt32
    let texture: Texture

    // Keep the dashboard's reference palette and its per-channel rounding.
    var palette: [UInt32] {
        if rgb == 0x2F6FED { return [0x0263D9, 0x65A6EB, 0x9CC0F0] }
        func mixed(target: Double, amount: Double) -> UInt32 {
            func component(_ shift: UInt32) -> UInt32 {
                let value = Double((rgb >> shift) & 0xFF)
                return UInt32((value + (target - value) * amount).rounded())
            }
            return (component(16) << 16) | (component(8) << 8) | component(0)
        }
        return [mixed(target: 0, amount: 0.2), mixed(target: 255, amount: 0.34),
                mixed(target: 255, amount: 0.65)]
    }

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

// Only scalar state crosses the renderer boundary; no audio samples or call data.
struct ReferenceShaderState: Equatable, Encodable {
    let colors: [String]
    let texture: String
    let mode: String
    let intensity: Double
    let paused: Bool

    init(theme: AnimatedAgentTheme, speaking: Bool, connected: Bool, energy: Float,
         reduceMotion: Bool, sceneActive: Bool, visible: Bool) {
        colors = theme.palette.map { String(format: "#%06x", $0) }
        texture = theme.texture.rawValue
        paused = reduceMotion || !sceneActive || !visible
        intensity = AnimatedAgentTheme.speechIntensity(speaking: speaking && connected && visible,
                                                       energy: energy, reduceMotion: reduceMotion,
                                                       sceneActive: sceneActive)
        mode = intensity > 0 ? "talking" : "listening"
    }
}
