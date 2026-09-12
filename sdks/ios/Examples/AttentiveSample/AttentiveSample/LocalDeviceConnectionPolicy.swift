import Foundation

/// Explicit sample-only LAN testing. Never changes a production signaling endpoint.
struct LocalDeviceConnectionPolicy: Sendable {
    let enabled: Bool
    private let host: String?

    init(endpoint: URL, environment: [String: String] = SampleLocalLaunchSettings.environment()) {
        host = endpoint.host
        #if DEBUG
        enabled = environment["ATTENTIVE_LOCAL_DEVICE"] == "1"
            && endpoint.scheme == "http" && [3000, 3004].contains(endpoint.port ?? -1)
            && endpoint.user == nil && endpoint.password == nil
            && endpoint.fragment == nil && Self.isPrivateIPv4(endpoint.host ?? "")
        #else
        enabled = false
        #endif
    }

    func signalingURL(_ original: URL) -> URL {
        guard enabled, original.scheme == "ws", original.port == 7880,
              ["localhost", "127.0.0.1", "[::1]"].contains(original.host?.lowercased() ?? ""),
              var components = URLComponents(url: original, resolvingAgainstBaseURL: false) else { return original }
        components.host = host
        return components.url ?? original
    }

    private static func isPrivateIPv4(_ host: String) -> Bool {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4, parts.allSatisfy({ !$0.isEmpty && $0.allSatisfy({ $0.isASCII && $0.isNumber }) }) else { return false }
        let octets = parts.compactMap { Int($0) }
        guard octets.count == 4, octets.allSatisfy({ (0...255).contains($0) }) else { return false }
        return octets[0] == 10 || (octets[0] == 172 && (16...31).contains(octets[1]))
            || (octets[0] == 192 && octets[1] == 168)
    }
}

/// Preserve only explicitly approved Debug routing when opening from the Home Screen.
enum SampleLocalLaunchSettings {
    private static let key = "attentive.sample.localLaunch"
    private static let routingKeys: Set<String> = [
        "ATTENTIVE_CALL_ENDPOINT", "ATTENTIVE_BUSINESS_SLUG",
        "ATTENTIVE_AGENT_ID", "ATTENTIVE_LOCAL_DEVICE",
    ]

    static func environment(
        _ launch: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard
    ) -> [String: String] {
        #if DEBUG
        if launch["ATTENTIVE_CALL_ENDPOINT"] != nil || launch["ATTENTIVE_LOCAL_DEVICE"] != nil {
            if let url = URL(string: launch["ATTENTIVE_CALL_ENDPOINT"] ?? ""),
               LocalDeviceConnectionPolicy(endpoint: url, environment: launch).enabled {
                defaults.set(launch.filter { routingKeys.contains($0.key) }, forKey: key)
            } else {
                defaults.removeObject(forKey: key)
            }
            return launch
        }
        guard let saved = defaults.dictionary(forKey: key) as? [String: String],
              let url = URL(string: saved["ATTENTIVE_CALL_ENDPOINT"] ?? ""),
              LocalDeviceConnectionPolicy(endpoint: url, environment: saved).enabled else { return launch }
        return saved.filter { routingKeys.contains($0.key) }.merging(launch) { _, current in current }
        #else
        return launch
        #endif
    }
}
