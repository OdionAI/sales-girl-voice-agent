#if os(iOS)
import SwiftUI

/// Presentation only. Agent identity, prompts, authorization and tools remain server-owned.
@available(iOS 17, *)
public struct CallerUIConfiguration {
    public var title: String
    public var agentName: String
    public var profileTitle: String
    public var customerIDLabel: String
    public var activityTitle: String
    public var emptyActivityText: String
    public var showsProfile: Bool
    public var showsToolWaitSelection: Bool
    public var showsAccountMenu: Bool = false
    public var showsCallOptions: Bool = false
    public var toolDisplayNames: [String: String]
    public var accentColor: Color
    public var avatar: Image?
    /// Dashboard-selected avatar theme. A host-supplied avatar takes precedence.
    public var themeKey: String?
    /// Set false only when the host deliberately retains the call after dismissing the UI.
    public var endsCallOnDisappear: Bool

    public init(title: String = "Talk to your agent", agentName: String = "Agent",
                profileTitle: String = "My account", customerIDLabel: String = "Customer ID",
                activityTitle: String = "Activity", emptyActivityText: String = "No activity yet",
                showsProfile: Bool = false, showsToolWaitSelection: Bool = true,
                toolDisplayNames: [String: String] = [:],
                accentColor: Color = Color(red: 132.0 / 255, green: 39.0 / 255, blue: 35.0 / 255),
                avatar: Image? = nil, themeKey: String? = nil, endsCallOnDisappear: Bool = true) {
        self.title = title
        self.agentName = agentName
        self.profileTitle = profileTitle
        self.customerIDLabel = customerIDLabel
        self.activityTitle = activityTitle
        self.emptyActivityText = emptyActivityText
        self.showsProfile = showsProfile
        self.showsToolWaitSelection = showsToolWaitSelection
        self.toolDisplayNames = toolDisplayNames
        self.accentColor = accentColor
        self.avatar = avatar
        self.themeKey = themeKey
        self.endsCallOnDisappear = endsCallOnDisappear
    }
}
#endif
