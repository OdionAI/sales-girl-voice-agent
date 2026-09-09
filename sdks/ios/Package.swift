// swift-tools-version: 6.1
import PackageDescription

let package = Package(
    name: "AttentiveVoice",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "AttentiveVoice", targets: ["AttentiveVoice"]),
        .library(name: "AttentiveVoiceUI", targets: ["AttentiveVoiceUI"]),
        .executable(name: "attentive-smoke", targets: ["AttentiveSmoke"]),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-protobuf.git", exact: "1.38.1"),
    ],
    targets: [
        .target(name: "AttentiveVoice", dependencies: [
            "AttentiveRTC",
        ]),
        .target(name: "AttentiveVoiceUI", dependencies: ["AttentiveVoice"],
                resources: [.process("Resources")]),
        .executableTarget(name: "AttentiveSmoke", dependencies: ["AttentiveVoice"]),
        .testTarget(name: "AttentiveVoiceTests", dependencies: ["AttentiveVoice"]),
        .testTarget(name: "AttentiveVoiceUITests", dependencies: ["AttentiveVoiceUI", "AttentiveVoice"]),
        // Private transport targets, not customer-facing library products.
        .target(name: "AttentiveRTC", dependencies: [
            "AttentiveObjCHelpers", "AttentiveMediaBindings", "LiveKitWebRTC",
            .product(name: "SwiftProtobuf", package: "swift-protobuf"),
        ], path: "Vendor/AttentiveRTC/Sources/AttentiveRTC",
                exclude: ["Broadcast/NOTICE"],
                resources: [.process("PrivacyInfo.xcprivacy"), .copy("ThirdPartyNotices")],
                swiftSettings: [.swiftLanguageMode(.v6)]),
        .target(name: "AttentiveObjCHelpers",
                path: "Vendor/AttentiveRTC/Sources/AttentiveObjCHelpers",
                publicHeadersPath: "include"),
        .target(name: "AttentiveMediaBindings", dependencies: ["RustLiveKitUniFFI"],
                path: "Vendor/AttentiveRTC/Sources/AttentiveMediaBindings",
                swiftSettings: [.swiftLanguageMode(.v6)]),
        // Preserve the pinned native ABI. See Vendor/AttentiveRTC/upstream.json.
        .binaryTarget(name: "LiveKitWebRTC",
                url: "https://github.com/livekit/webrtc-xcframework/releases/download/144.7559.11/LiveKitWebRTC.xcframework.zip",
                checksum: "07c5caf718058af3c528dcabd257298c40e5a8527e4fb9f47c48336ba5899853"),
        .binaryTarget(name: "RustLiveKitUniFFI",
                url: "https://github.com/livekit/livekit-uniffi-xcframework/releases/download/0.0.6/RustLiveKitUniFFI.xcframework.zip",
                checksum: "0d3f2ce159a224c728f8b131068d53bbf9b13d968cda0edc68a6a2290f2651ed"),
    ],
    swiftLanguageModes: [.v5]
)
