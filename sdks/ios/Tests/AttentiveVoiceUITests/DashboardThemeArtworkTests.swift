#if os(iOS)
import AttentiveVoice
import SwiftUI
import UIKit
import XCTest
@testable import AttentiveVoiceUI

@MainActor
final class DashboardThemeArtworkTests: XCTestCase {
    func testDashboardArtworkUsesTheSameCenteredCrop() throws {
        // Reference colors sampled from the dashboard SVGs' centered square crops.
        // These points differ substantially if the original wide images are squeezed.
        let themes: [(String, [UInt8], [UInt8])] = [
            ("default", [181, 148, 179], [96, 130, 139]),
            ("theme_2", [19, 34, 67], [176, 166, 164]),
            ("theme_3", [175, 33, 59], [219, 78, 69]),
        ]
        let call = AttentiveCall(apiKey: "test", agentID: "test")
        for (key, first, second) in themes {
            let avatar = CallerAvatar(speaking: false, connected: false, image: nil,
                                      themeKey: key, audioLevel: call.agentAudioLevel,
                                      animationReady: .constant(true))
            let renderer = ImageRenderer(content: avatar)
            renderer.scale = 1
            let image = try XCTUnwrap(renderer.uiImage?.cgImage)
            for (point, expected) in [((77, 90), first), ((127, 120), second)] {
                let pixel = try color(at: point, in: image)
                for channel in 0..<3 {
                    XCTAssertEqual(Int(pixel[channel]), Int(expected[channel]), accuracy: 18,
                                   "\(key) crop mismatch at \(point), channel \(channel)")
                }
            }
        }
    }

    private func color(at point: (Int, Int), in image: CGImage) throws -> [UInt8] {
        let pixel = try XCTUnwrap(image.cropping(to: CGRect(x: point.0, y: point.1, width: 1, height: 1)))
        var bytes = [UInt8](repeating: 0, count: 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: 1, height: 1, bitsPerComponent: 8,
            bytesPerRow: 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(pixel, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        return bytes
    }
}
#endif
