// swift-tools-version: 6.1
import PackageDescription

let package = Package(
    name: "AttentiveVoice",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "AttentiveVoice", targets: ["AttentiveVoice"]),
        .executable(name: "attentive-smoke", targets: ["AttentiveSmoke"]),
    ],
    dependencies: [
        .package(url: "https://github.com/livekit/client-sdk-swift.git", exact: "2.16.0"),
    ],
    targets: [
        .target(name: "AttentiveVoice", dependencies: [
            .product(name: "LiveKit", package: "client-sdk-swift"),
        ]),
        .executableTarget(name: "AttentiveSmoke", dependencies: ["AttentiveVoice"]),
        .testTarget(name: "AttentiveVoiceTests", dependencies: ["AttentiveVoice"]),
    ],
    swiftLanguageModes: [.v5]
)
