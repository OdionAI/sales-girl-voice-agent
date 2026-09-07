#!/usr/bin/env node
// Offline fork integrity and customer-facing package-boundary checks.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const sdk = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const vendor = path.join(sdk, 'Vendor/AttentiveRTC');
const readJSON = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const pins = readJSON(path.join(vendor, 'upstream.json'));
const inventory = readJSON(path.join(vendor, 'inventory.json'));
const recorded = new Set();
for (const entry of inventory) {
  assert(!recorded.has(entry.destination), `Duplicate inventory entry: ${entry.destination}`);
  recorded.add(entry.destination);
  const bytes = fs.readFileSync(path.join(vendor, entry.destination));
  assert.equal(createHash('sha256').update(bytes).digest('hex'), entry.sha256,
    `Fork drift: ${entry.destination}`);
}
for (const directory of ['Sources', 'Licenses']) {
  for (const relative of fs.readdirSync(path.join(vendor, directory), { recursive: true })) {
    const file = `${directory}/${relative}`;
    if (fs.statSync(path.join(vendor, file)).isFile()) assert(recorded.has(file), `Unaudited file: ${file}`);
  }
}

const manifest = JSON.parse(execFileSync('swift', ['package', '--package-path', sdk, 'dump-package'],
  { encoding: 'utf8', maxBuffer: 1024 * 1024 }));
assert.deepEqual(manifest.products.filter(product => product.type.library).map(product => product.name).sort(),
  ['AttentiveVoice', 'AttentiveVoiceUI']);
assert.equal(manifest.dependencies.length, 1, 'Do not reintroduce external transport source packages');
const dependency = manifest.dependencies[0].sourceControl[0];
assert.equal(dependency.identity, 'swift-protobuf');
assert.equal(dependency.location.remote[0].urlString, pins.protobuf.repository);
assert.deepEqual(dependency.requirement.exact, [pins.protobuf.version]);
for (const lockFile of ['Package.resolved',
  'Examples/AttentiveSample/AttentiveSample.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved']) {
  const resolved = readJSON(path.join(sdk, lockFile));
  assert.deepEqual(resolved.pins.map(pin => [pin.identity, pin.state.version, pin.state.revision]),
    [['swift-protobuf', pins.protobuf.version, pins.protobuf.revision]], `Unexpected dependency in ${lockFile}`);
}

const targets = new Map(manifest.targets.map(target => [target.name, target]));
assert.deepEqual(targets.get('AttentiveVoice').dependencies, [{ byName: ['AttentiveRTC', null] }]);
assert.deepEqual(targets.get('AttentiveVoiceUI').dependencies, [{ byName: ['AttentiveVoice', null] }]);
assert.equal(manifest.targets.filter(target => target.type === 'binary').length, pins.artifacts.length);
for (const artifact of pins.artifacts) {
  assert.equal(targets.get(artifact.name)?.url, artifact.url);
  assert.equal(targets.get(artifact.name)?.checksum, artifact.checksum);
}
assert(targets.get('AttentiveRTC').resources.some(resource => resource.path === 'ThirdPartyNotices'));
assert(targets.get('AttentiveRTC').resources.some(resource => resource.path === 'PrivacyInfo.xcprivacy'));

for (const relative of fs.readdirSync(path.join(sdk, 'Sources'), { recursive: true })) {
  if (!relative.endsWith('.swift')) continue;
  const text = fs.readFileSync(path.join(sdk, 'Sources', relative), 'utf8');
  assert(!/\bimport\s+(?:LiveKit\w*|RustLiveKitUniFFI)\b/.test(text), `Upstream import in adapter: ${relative}`);
  if (/\bimport\s+AttentiveRTC\b/.test(text)) {
    assert.equal(relative, 'AttentiveVoice/RealtimeCallTransport.swift');
    assert(text.includes('internal import AttentiveRTC'), 'Transport types must not leak into public API');
  }
}
assert(!fs.existsSync(path.join(sdk, 'Sources/AttentiveVoice/LiveKitCallTransport.swift')));
console.log(`PASS: ${inventory.length} fork files; Attentive-only public products; native pins and API boundaries intact.`);
