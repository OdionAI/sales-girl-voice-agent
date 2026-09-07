#!/usr/bin/env node
// Reproduce or verify the pinned, namespace-only transport fork from git objects.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const sdk = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const vendor = path.join(sdk, 'Vendor/AttentiveRTC');
const pins = JSON.parse(fs.readFileSync(path.join(vendor, 'upstream.json'), 'utf8'));
const [mode, checkoutRoot, nativeLicensePath] = process.argv.slice(2);
assert(['--write', '--check'].includes(mode) && checkoutRoot,
  'Usage: node script/vendor_transport.mjs --write|--check /path/to/pinned-checkouts');

const replacements = [
  [/\bLiveKitSDK\b/g, 'AttentiveRTCSDK'],
  [/\bLKObjCHelpers\b/g, 'AttentiveObjCHelpers'],
  [/\bLiveKitUniFFI\b/g, 'AttentiveMediaBindings'],
];
const expected = new Map();
const inventory = [];
const sha256 = data => createHash('sha256').update(data).digest('hex');

function git(component, ...args) {
  return execFileSync('git', ['-C', path.resolve(checkoutRoot, pins[component].checkout), ...args],
    { maxBuffer: 16 * 1024 * 1024 });
}

function copy(component, source, destination, transform = false) {
  const original = git(component, 'show', `${pins[component].revision}:${source}`);
  let content = original;
  if (transform) {
    let text = original.toString('utf8');
    for (const [pattern, replacement] of replacements) text = text.replace(pattern, replacement);
    if (text !== original.toString('utf8')) {
      text = '// Modified by Odion AI for Attentive: namespace-only changes; see upstream.json.\n' + text;
    }
    content = Buffer.from(text);
  }
  assert(!expected.has(destination), `Duplicate destination: ${destination}`);
  expected.set(destination, content);
  inventory.push({ component, source, destination, upstreamSHA256: sha256(original), sha256: sha256(content) });
}

// Read committed objects, not potentially edited dependency checkout files.
const sources = git('client', 'ls-tree', '-rz', '--name-only', pins.client.revision, 'Sources')
  .toString('utf8').split('\0').filter(Boolean).sort();
for (const source of sources) {
  if (source.includes('/LiveKit.docc/')) continue;
  const destination = source
    .replace('Sources/LiveKit/', 'Sources/AttentiveRTC/')
    .replaceAll('LKObjCHelpers', 'AttentiveObjCHelpers')
    .replace('/LiveKit.swift', '/AttentiveRTC.swift')
    .replace('/LiveKit+DeviceHelpers.swift', '/AttentiveRTC+DeviceHelpers.swift');
  copy('client', source, destination, /\.(swift|h|m|modulemap)$/.test(source));
}
copy('client', 'LICENSE', 'LICENSE');
copy('client', 'NOTICE', 'NOTICE');
copy('bindings', 'Sources/LiveKitUniFFI/livekit_uniffi.swift', 'Sources/AttentiveMediaBindings/MediaBindings.swift');
copy('bindings', 'LICENSE', 'Licenses/MediaBindings-LICENSE');
copy('bindings', 'PrivacyInfo.xcprivacy', 'Licenses/MediaBindings-PrivacyInfo.xcprivacy');
copy('webrtc', 'LICENSE', 'Licenses/WebRTC-Package-LICENSE');
copy('protobuf', 'LICENSE.txt', 'Licenses/SwiftProtobuf-LICENSE');

const nativeLicense = fs.readFileSync(nativeLicensePath ?? path.join(sdk,
  '.build/artifacts/ios/LiveKitWebRTC/LiveKitWebRTC.xcframework/LICENSE'));
assert.equal(sha256(nativeLicense), pins.artifacts[0].licenseSHA256, 'Native WebRTC license differs from pinned artifact');
expected.set('Licenses/WebRTC-Binary-LICENSE', nativeLicense);
inventory.push({ component: 'webrtc', source: 'LiveKitWebRTC.xcframework/LICENSE',
  destination: 'Licenses/WebRTC-Binary-LICENSE', upstreamSHA256: sha256(nativeLicense), sha256: sha256(nativeLicense) });

// Carry upstream attribution into the app's resource bundle as well as source.
for (const [source, content] of [...expected]) {
  if (source === 'LICENSE' || source === 'NOTICE' || source.endsWith('/NOTICE') || source.endsWith('-LICENSE')) {
    const destination = `Sources/AttentiveRTC/ThirdPartyNotices/${source === 'Sources/AttentiveRTC/Broadcast/NOTICE' ? 'Broadcast-NOTICE' : path.basename(source)}`;
    expected.set(destination, content);
    inventory.push({ ...inventory.find(entry => entry.destination === source), destination });
  }
}

inventory.sort((a, b) => a.destination.localeCompare(b.destination, 'en'));
expected.set('inventory.json', Buffer.from(JSON.stringify(inventory, null, 2) + '\n'));

// Refuse to overwrite a local fork edit. Updating the fork requires a reviewed
// change to the import rules/pins, not silently replacing someone's source.
for (const [relative, content] of expected) {
  const destination = path.join(vendor, relative);
  if (fs.existsSync(destination)) {
    if (relative === 'inventory.json' && mode === '--write') {
      for (const entry of JSON.parse(fs.readFileSync(destination, 'utf8'))) {
        assert.equal(sha256(expected.get(entry.destination)), entry.sha256, `Existing inventory differs: ${entry.destination}`);
      }
      continue;
    }
    assert(fs.readFileSync(destination).equals(content), `Content differs: ${relative}`);
  } else {
    assert(mode === '--write', `Missing: ${relative}`);
  }
}
if (mode === '--write') {
  for (const [relative, content] of expected) {
    const destination = path.join(vendor, relative);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, content);
  }
}
for (const directory of ['Sources', 'Licenses']) {
  for (const relative of fs.readdirSync(path.join(vendor, directory), { recursive: true })) {
    const file = `${directory}/${relative}`;
    if (fs.statSync(path.join(vendor, file)).isFile()) assert(expected.has(file), `Unexpected fork file: ${file}`);
  }
}
console.log(`${mode === '--write' ? 'Imported' : 'Verified'} ${inventory.length} pinned files; namespace-only fork.`);
