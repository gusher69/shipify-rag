// Regression test for the AI Playground session-selection bug: selecting
// a 3rd+ session checkbox used to silently uncheck an earlier one because
// toggleCompareSelect() capped _compareSelection at 2 entries (a rule
// meant only for the "Compare exactly 2 sessions" feature, but the same
// checkbox/array is shared with bulk-Delete). This test extracts the
// ACTUAL functions from admin/templates/preview.html (not a hand-copied
// re-implementation) so a regression of the cap is caught here, not just
// eyeballed in the browser.
//
// Run with: node tests/js/test_session_selection.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const templatePath = path.join(__dirname, '..', '..', 'admin', 'templates', 'preview.html');
const html = fs.readFileSync(templatePath, 'utf8');

function extractFunction(name) {
  const start = html.indexOf(`function ${name}(`);
  assert(start !== -1, `function ${name} not found in preview.html`);
  let depth = 0, i = html.indexOf('{', start), bodyStart = i;
  for (; i < html.length; i++) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}') { depth--; if (depth === 0) break; }
  }
  return html.slice(start, i + 1);
}

// Minimal DOM/browser stub — just enough for these two functions.
const fakeElements = {};
function fakeGetElementById(id) {
  if (!fakeElements[id]) fakeElements[id] = { textContent: '' };
  return fakeElements[id];
}

const sandbox = {
  document: { getElementById: fakeGetElementById, querySelectorAll: () => [] },
  console,
  _compareSelection: [],
  _sessionId: null,
  lastIdsToDelete: null,
  selectSession: function (id) { sandbox._sessionId = id; }, // stub — real one does a fetch
  createNewSession: function () {}, // stub — real one does a fetch
};
vm.createContext(sandbox);

const toggleSrc = extractFunction('toggleCompareSelect');
const deleteSrc = extractFunction('deleteCurrentSession');
// deleteCurrentSession normally calls fetch()/Promise.all — stub fetch so
// we can call it and inspect idsToDelete without a real network/DOM.
const deleteSrcStubbed = deleteSrc
  .replace('Promise.all(idsToDelete.map(function(id){', 'lastIdsToDelete = idsToDelete.slice(); Promise.all(idsToDelete.map(function(id){')
  .replace("return fetch('/admin/playground/sessions/' + id, { method: 'DELETE' });", "return Promise.resolve();")
  .replace('loadSessions();', '');

vm.runInContext(`
  ${toggleSrc}
  ${deleteSrcStubbed}
  var confirm = function() { return true; };
  var alert = function() {};
`, sandbox);

function reset() {
  sandbox._compareSelection = [];
  sandbox._sessionId = null;
}

// ── Test 1: select 1 session ──
reset();
sandbox.toggleCompareSelect('s1', true);
assert.deepStrictEqual(sandbox._compareSelection, ['s1']);
console.log('PASS: select 1 session');

// ── Test 2: select 5 sessions — all must remain checked ──
reset();
['s1', 's2', 's3', 's4', 's5'].forEach(id => sandbox.toggleCompareSelect(id, true));
assert.deepStrictEqual(sandbox._compareSelection, ['s1', 's2', 's3', 's4', 's5'],
  'selecting a 4th/5th session must not evict earlier selections');
console.log('PASS: select 5 sessions (no eviction of earlier picks)');

// ── Test 3: select 20 sessions — unlimited selection ──
reset();
const twenty = Array.from({ length: 20 }, (_, i) => 's' + i);
twenty.forEach(id => sandbox.toggleCompareSelect(id, true));
assert.strictEqual(sandbox._compareSelection.length, 20, 'must support selecting more than 3 sessions');
assert.deepStrictEqual(sandbox._compareSelection, twenty);
console.log('PASS: select 20 sessions (unlimited selection)');

// ── Test 4: select all, then unselect one ──
reset();
twenty.forEach(id => sandbox.toggleCompareSelect(id, true));
sandbox.toggleCompareSelect('s10', false);
assert.strictEqual(sandbox._compareSelection.length, 19);
assert.strictEqual(sandbox._compareSelection.indexOf('s10'), -1);
assert.strictEqual(sandbox._compareSelection.indexOf('s0'), 0, 'unselecting one must not disturb the others');
console.log('PASS: unselect one out of many leaves the rest intact');

// ── Test 5: delete sends ALL selected IDs, not just the last few ──
reset();
twenty.forEach(id => sandbox.toggleCompareSelect(id, true));
sandbox.deleteCurrentSession();
assert.strictEqual(sandbox.lastIdsToDelete.length, 20, 'Delete must send every selected id');
assert.deepStrictEqual(sandbox.lastIdsToDelete.slice().sort(), twenty.slice().sort());
console.log('PASS: delete sends all 20 selected ids');

console.log('\nAll session-selection tests passed.');
