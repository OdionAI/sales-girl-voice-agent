#if os(iOS)
import AttentiveVoice
import SwiftUI

// Matches the public web call-surface and wema-caller-console, not Voice Lab.
enum CallerTheme {
    static let stage = Color(hex: 0xFBFAF8)
    static let surface = Color(hex: 0xFDFCFA)
    static let ink = Color(hex: 0x241936)
    static let muted = Color(hex: 0x7D7592)
    static let border = Color(hex: 0xE4E0DB)
    static let divider = Color(hex: 0xECE8E2)
    static let field = Color(hex: 0xF4F1EE)
    static let control = Color(hex: 0xECEAE9)
    static let accent = Color(hex: 0x842723)
    static let endFill = Color(hex: 0xFFB4B6)
    static let endInk = Color(hex: 0x8F181F)
    static let red = Color(hex: 0xB33A32)
    static let green = Color(hex: 0x278564)
}

private extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 255) / 255,
                  green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255)
    }
}

struct CallerIconStyle: ButtonStyle {
    var tint = CallerTheme.ink
    var selected = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 18)).foregroundStyle(tint).frame(width: 44, height: 44)
            .background(selected ? CallerTheme.border : CallerTheme.control, in: Circle())
            .overlay(Circle().stroke(CallerTheme.border, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.65 : 1)
    }
}

struct CallerAvatar: View {
    let speaking: Bool
    let connected: Bool
    let image: Image?
    let themeKey: String?
    @ObservedObject var audioLevel: AgentAudioLevel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    private var scale: Double {
        guard image == nil, AnimatedAgentTheme(key: themeKey) != nil else {
            return speaking && !reduceMotion ? 1 + Double(audioLevel.energy) * 0.25 : 1
        }
        return 1 + intensity * 0.10
    }

    private var intensity: Double {
        AnimatedAgentTheme.speechIntensity(speaking: speaking && connected, energy: audioLevel.energy,
                                           reduceMotion: reduceMotion, sceneActive: scenePhase == .active)
    }

    private var assetName: String {
        switch themeKey?.lowercased() {
        case "theme_2": return "CallerAvatarTheme2"
        case "theme_3": return "CallerAvatarTheme3"
        default: return "CallerAvatar"
        }
    }

    private var fillColor: Color? {
        guard let value = themeKey?.lowercased(), value.hasPrefix("color:#"), value.count == 13,
              let hex = UInt32(value.dropFirst("color:#".count), radix: 16) else { return nil }
        return Color(hex: hex)
    }

    var body: some View {
        Group {
            if let image {
                image.resizable().scaledToFill()
            } else if let animated = AnimatedAgentTheme(key: themeKey) {
                AnimatedCallerAvatar(theme: animated, intensity: intensity,
                                    paused: reduceMotion || scenePhase != .active,
                                    reduceMotion: reduceMotion)
            } else if let fillColor {
                fillColor
            } else {
                Image(assetName, bundle: .module).resizable().scaledToFill()
            }
        }
            .frame(width: 123, height: 123).clipShape(Circle()).scaleEffect(scale)
            .animation(reduceMotion ? nil : .interpolatingSpring(mass: 0.68, stiffness: 300, damping: 22), value: scale)
            .frame(width: 204, height: 204).accessibilityHidden(true)
    }
}

// Native gradients are drawn directly, without external assets or a shader dependency.
private struct AnimatedCallerAvatar: View {
    let theme: AnimatedAgentTheme
    let intensity: Double
    let paused: Bool
    let reduceMotion: Bool
    @State private var isVisible = false

    private var rgb: (Double, Double, Double) {
        (Double((theme.rgb >> 16) & 255) / 255, Double((theme.rgb >> 8) & 255) / 255,
         Double(theme.rgb & 255) / 255)
    }

    private func color(mixedWith target: Double, amount: Double) -> Color {
        let (r, g, b) = rgb
        return Color(.sRGB, red: r + (target - r) * amount,
                     green: g + (target - g) * amount, blue: b + (target - b) * amount)
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: paused || !isVisible)) { timeline in
            let phase = reduceMotion ? 0 : timeline.date.timeIntervalSinceReferenceDate * 0.3
            Canvas { context, size in
                let rect = CGRect(origin: .zero, size: size)
                let dark = color(mixedWith: 0, amount: 0.68)
                let base = color(mixedWith: 0, amount: 0)
                let light = color(mixedWith: 1, amount: 0.72)
                context.fill(Path(rect), with: .color(dark))
                let displacement = 0.035 * intensity
                glow(context: &context, rect: rect, color: base,
                     x: 0.42 + 0.15 * sin(phase) + displacement,
                     y: 0.38 + 0.12 * cos(phase * 0.8), radius: 0.8)
                glow(context: &context, rect: rect, color: light,
                     x: 0.66 + 0.12 * cos(phase * 0.7),
                     y: 0.63 + 0.14 * sin(phase * 0.9) - displacement, radius: 0.65)
                glow(context: &context, rect: rect, color: dark,
                     x: 0.12 + 0.08 * sin(phase * 1.2), y: 0.84, radius: 0.62)
                if theme.texture == .grain { grain(context: &context, size: size) }
            }
        }
        .onAppear { isVisible = true }
        .onDisappear { isVisible = false }
    }

    private func glow(context: inout GraphicsContext, rect: CGRect, color: Color,
                      x: Double, y: Double, radius: Double) {
        let center = CGPoint(x: rect.width * x, y: rect.height * y)
        let gradient = Gradient(colors: [color.opacity(0.95), color.opacity(0)])
        context.fill(Path(rect), with: .radialGradient(gradient, center: center,
                                                       startRadius: 0, endRadius: rect.width * radius))
    }

    private func grain(context: inout GraphicsContext, size: CGSize) {
        // Stable, deterministic grain does not flicker while the gradients move.
        var seed: UInt32 = 19
        for _ in 0..<500 {
            seed = seed &* 1664525 &+ 1013904223
            let x = CGFloat(seed & 0xffff) / 65535 * size.width
            seed = seed &* 1664525 &+ 1013904223
            let y = CGFloat(seed & 0xffff) / 65535 * size.height
            let color: Color = (seed & 0x10000) == 0 ? .white : .black
            context.fill(Path(CGRect(x: x, y: y, width: 0.7, height: 0.7)), with: .color(color.opacity(0.10)))
        }
    }
}
#endif
