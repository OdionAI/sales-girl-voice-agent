# Voice accent and gender

The next SDK release adds code-only speaking-voice preferences. The supplied
caller has no accent or gender controls. Set these before presenting it; dismiss
and start a new call to change the preference.

<!-- snippet: voice-preferences -->
```swift
import AttentiveVoice
import AttentiveVoiceUI
import SwiftUI

@available(iOS 17, *)
@MainActor
struct VoicePreferencesExample: View {
    let fetchCallerToken: () async throws -> String

    var body: some View {
        AttentiveAgentView(
            apiKey: "YOUR_API_KEY",
            agentID: "YOUR_AGENT_ID",
            customerID: "YOUR_CUSTOMER_ID",
            voiceLanguage: .enNG,
            voiceGender: .female,
            callerToken: fetchCallerToken
        )
    }
}
```

- `VoiceAccent.enNG` encodes as `en-NG`; `.enUS` encodes as `en-US`.
- `VoiceGender.female` and `.male` encode as `female` and `male`.
- Both parameters default to `nil`: omitted values keep the service's selection,
  preserving existing call requests. The Wema voice-settings contract specifies
  Nigerian English / female as its server default. Other deployments own their defaults.
- `voiceLanguage` selects the speaking accent; it does not replace or change
  `CallRequest.language` (the runtime agent/prompt language).
- `audioInput`, passkeys and caller authentication are independent and unchanged.
- The compatible service resolves the voice. The SDK sends no voice ID or model key.

The API-key `AttentiveCall` initializers accept the same optional parameters.
For an endpoint or custom credential provider, set them per `CallRequest`:

```swift
let request = CallRequest(
    businessSlug: "YOUR_BUSINESS_SLUG",
    agentPublicId: "YOUR_AGENT_ID",
    endUserContact: "caller@example.com",
    language: "en",
    voiceLanguage: .enUS,
    voiceGender: .male
)
try await call.start(request)
```

Both HTTP paths send the exact camelCase keys `voiceLanguage` and `voiceGender`
when supplied and omit nil values. The dashboard configuration request does not
include voice preferences. Invalid enum strings fail decoding instead of being
silently accepted.

A matching voice must be provisioned and the service must support both fields.
Successful encoding or a connected call does not confirm the selected voice is
active: test all four combinations audibly against that deployment.
