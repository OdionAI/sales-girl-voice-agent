#!/usr/bin/env bash
set -euo pipefail

sample_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sdk_root="$(cd "${sample_root}/../.." && pwd)"
: "${ATTENTIVE_DEVICE_ID:?Set the physical iPhone UDID from devicectl device info details}"
: "${ATTENTIVE_DEVELOPMENT_TEAM:?Set your Apple development team ID}"
: "${ATTENTIVE_CALL_ENDPOINT:?Set a reachable HTTPS endpoint or the approved private LAN test endpoint}"
derived="${sdk_root}/.build/physical-app"

xcodebuild -project "${sample_root}/AttentiveSample.xcodeproj" -scheme AttentiveSample \
  -configuration Debug -destination "id=${ATTENTIVE_DEVICE_ID}" -derivedDataPath "${derived}" \
  -clonedSourcePackagesDirPath "${sdk_root}/.build" -jobs 4 \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  "DEVELOPMENT_TEAM=${ATTENTIVE_DEVELOPMENT_TEAM}" build
xcrun devicectl device install app --device "${ATTENTIVE_DEVICE_ID}" \
  "${derived}/Build/Products/Debug-iphoneos/AttentiveSample.app"

for key in ATTENTIVE_CALL_ENDPOINT ATTENTIVE_BUSINESS_SLUG ATTENTIVE_AGENT_ID \
           ATTENTIVE_CALLER_CONTACT ATTENTIVE_CUSTOMER_ID ATTENTIVE_PHONE ATTENTIVE_ACCOUNT ATTENTIVE_LOCAL_DEVICE; do
  if [[ -n "${!key:-}" ]]; then
    export "DEVICECTL_CHILD_${key}=${!key}"
  fi
done
# Refuse to stop an existing test call implicitly. End/close it before rerunning.
xcrun devicectl device process launch --device "${ATTENTIVE_DEVICE_ID}" ai.odion.attentive.sample "$@"
