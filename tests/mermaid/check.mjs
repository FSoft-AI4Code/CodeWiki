import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.Element = dom.window.Element;
const { default: mermaid } = await import('mermaid');
mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
const fixtures = JSON.parse(readFileSync(new URL('./fixtures.json', import.meta.url)));
for (const fixture of fixtures) {
  let valid = true;
  try {
    await mermaid.parse(fixture.source);
  } catch {
    valid = false;
  }
  assert.equal(valid, fixture.valid, fixture.name);
  if (fixture.repaired) {
    assert.ok(await mermaid.parse(fixture.repaired), `${fixture.name}: repaired`);
  }
}
console.log(`${fixtures.length} Mermaid 11.9.0 parser fixtures passed`);
