#if os(iOS)
import AttentiveVoice
import SwiftUI
import UIKit

// The light palette matches the public caller. Semantic colors adapt to the presented view's style.
enum CallerTheme {
    static let stage = Color(light: 0xFBFAF8, dark: 0x19161F)
    static let surface = Color(light: 0xFDFCFA, dark: 0x231E2B)
    static let raisedSurface = Color(light: 0xFFFFFF, dark: 0x2B2336)
    static let ink = Color(light: 0x241936, dark: 0xF5F0FB)
    static let onInk = Color(light: 0xFFFFFF, dark: 0x19161F)
    static let shadow = Color(light: 0x241936, dark: 0x000000)
    static let muted = Color(light: 0x7D7592, dark: 0xBFB4CD)
    static let border = Color(light: 0xE4E0DB, dark: 0x51465F)
    static let divider = Color(light: 0xECE8E2, dark: 0x40354B)
    static let field = Color(light: 0xF4F1EE, dark: 0x2E2637)
    static let control = Color(light: 0xECEAE9, dark: 0x342A3F)
    static let endFill = Color(light: 0xFFB4B6, dark: 0x512127)
    static let endInk = Color(light: 0x8F181F, dark: 0xFFB9BE)
    static let red = Color(light: 0xB33A32, dark: 0xF8867E)
    static let green = Color(light: 0x278564, dark: 0x70DBAF)
    static let avatarDiameter: CGFloat = 123
}

private extension Color {
    init(light: UInt32, dark: UInt32) {
        self.init(uiColor: UIColor { traits in
            let hex = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: CGFloat((hex >> 16) & 255) / 255,
                           green: CGFloat((hex >> 8) & 255) / 255,
                           blue: CGFloat(hex & 255) / 255, alpha: 1)
        })
    }

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
    @Binding var animationReady: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    private var scale: Double {
        guard connected, speaking, !reduceMotion, scenePhase == .active,
              audioLevel.energy > 0.01 else { return 1 }
        return 1 + Double(audioLevel.energy) * 0.25
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
            if image == nil, let animated = AnimatedAgentTheme(key: themeKey) {
                ReferenceAnimatedAvatar(theme: animated, speaking: speaking, connected: connected,
                                        energy: audioLevel.energy, reduceMotion: reduceMotion,
                                        sceneActive: scenePhase == .active, ready: $animationReady)
            } else {
                Group {
                    if let image {
                        image.resizable().scaledToFill()
                    } else if let fillColor {
                        fillColor
                    } else {
                        Image(assetName, bundle: .module).resizable().scaledToFill()
                    }
                }
                .frame(width: CallerTheme.avatarDiameter, height: CallerTheme.avatarDiameter)
                .clipShape(Circle())
                .scaleEffect(scale)
                .animation(reduceMotion ? nil : .interpolatingSpring(mass: 0.68, stiffness: 300, damping: 22), value: scale)
            }
        }
        .frame(width: 204, height: 204).accessibilityHidden(true)
    }
}
#endif
