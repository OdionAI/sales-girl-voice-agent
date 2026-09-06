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
    @ObservedObject var audioLevel: AgentAudioLevel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var scale: Double {
        speaking && !reduceMotion ? 1 + Double(audioLevel.energy) * 0.25 : 1
    }
    var body: some View {
        Image("CallerAvatar").resizable().scaledToFill()
            .frame(width: 123, height: 123).clipShape(Circle()).scaleEffect(scale)
            .animation(reduceMotion ? nil : .interpolatingSpring(mass: 0.68, stiffness: 300, damping: 22), value: scale)
            .frame(width: 204, height: 204).accessibilityHidden(true)
    }
}
