#!/usr/bin/env bash
set -euo pipefail

sample_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sdk_root="$(cd "${sample_root}/../.." && pwd)"
device="${ATTENTIVE_SIMULATOR_ID:-}"
if [[ -z "${device}" ]]; then
  device="$(xcrun simctl list devices available --json | /usr/bin/python3 -c '
import json, sys
devices = [d for group in json.load(sys.stdin)["devices"].values() for d in group if d["name"].startswith("iPhone")]
devices.sort(key=lambda d: d["state"] != "Booted")
if not devices:
    sys.exit("Install an iOS Simulator runtime in Xcode first.")
print(devices[0]["udid"])
')"
fi
derived="${sdk_root}/.build/sample-app"
bundle="ai.odion.attentive.sample"

if ! xcrun simctl list devices booted | grep -Fq "${device}"; then
  xcrun simctl boot "${device}"
fi
xcrun simctl bootstatus "${device}" -b
open -a Simulator
xcrun simctl terminate "${device}" "${bundle}" >/dev/null 2>&1 || true
xcodebuild -project "${sample_root}/AttentiveSample.xcodeproj" -scheme AttentiveSample \
  -configuration Debug -destination "id=${device}" -derivedDataPath "${derived}" \
  -clonedSourcePackagesDirPath "${sdk_root}/.build/sample-dependencies" -scmProvider system -jobs 4 CODE_SIGNING_ALLOWED=NO build
xcrun simctl install "${device}" "${derived}/Build/Products/Debug-iphonesimulator/AttentiveSample.app"

for key in ATTENTIVE_CALL_ENDPOINT ATTENTIVE_BUSINESS_SLUG ATTENTIVE_AGENT_ID \
           ATTENTIVE_CALLER_CONTACT ATTENTIVE_CUSTOMER_ID ATTENTIVE_PHONE ATTENTIVE_ACCOUNT; do
  if [[ -n "${!key:-}" ]]; then
    export "SIMCTL_CHILD_${key}=${!key}"
  fi
done
xcrun simctl launch "${device}" "${bundle}" "$@"
