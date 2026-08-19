// Regression test for the AI Playground "chat bubble shows raw JSON under
// Developer Mode" bug: a 2026-08-17 change made the bubble itself swap to
// d.erp.answer (a raw JSON string) whenever Developer Mode was on, which a
// customer test then mistook for an actual production leak. Fixed
// 2026-08-19 — the bubble must always show d.reply_text for mode=="auto",
// regardless of Developer Mode. This test extracts the ACTUAL
// computeDisplayAnswer() function from admin/templates/preview.html (not a
// hand-copied re-implementation) so a regression is caught here, not just
// eyeballed in the browser.
//
// Run with: node tests/js/test_bubble_display_answer.js
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

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(extractFunction('computeDisplayAnswer'), sandbox);

const rawErpJson = '{"รายการคำสั่งซื้อทั้งหมด": [{"Code": "POS100820260815001"}]}';
const naturalReply = 'เลขที่คำสั่งซื้อล่าสุด: POS100820260815001 ค่ะ';

// ── Test 1: Developer Mode is a template-render-time concern only —
// computeDisplayAnswer() takes no devModeEnabled parameter at all, so it
// is STRUCTURALLY incapable of branching on it. This is itself the
// regression guard: if a future change reintroduces a devModeEnabled
// parameter, this assertion on arity documents the intent even before
// any behavioral test below would catch it. ──
assert.strictEqual(sandbox.computeDisplayAnswer.length, 1,
  'computeDisplayAnswer must take only `d` — never a devModeEnabled flag');
console.log('PASS: computeDisplayAnswer has no Developer Mode parameter');

// ── Test 2: mode=="auto" always uses reply_text, even when the raw ERP
// debug payload (erp.answer) is present and JSON-shaped ──
let d = { mode: 'auto', reply_text: naturalReply, erp: { answer: rawErpJson } };
assert.strictEqual(sandbox.computeDisplayAnswer(d), naturalReply);
console.log('PASS: auto mode with erp.answer present still uses reply_text');

// ── Test 3: same input, simulating what the page looked like under the
// old bug (erp.answer must NEVER win over reply_text) ──
d = { mode: 'auto', reply_text: naturalReply, erp: { answer: rawErpJson }, hybrid: null, rag: null };
const result = sandbox.computeDisplayAnswer(d);
assert.notStrictEqual(result, rawErpJson, 'the raw JSON debug payload must never become the bubble content');
assert.ok(!result.trim().startsWith('{'), 'bubble text must never look like raw JSON');
console.log('PASS: raw erp.answer never becomes bubble content in auto mode');

// ── Test 4: mode=="auto" with hybrid data present — still reply_text, not
// hybrid.labeled_answer/merged_answer ──
d = { mode: 'auto', reply_text: naturalReply, hybrid: { labeled_answer: '📦 ...', merged_answer: '...' } };
assert.strictEqual(sandbox.computeDisplayAnswer(d), naturalReply);
console.log('PASS: auto mode with hybrid data present still uses reply_text');

// ── Test 5: mode=="auto" but reply_text is falsy — falls back to
// clarification message, never raw erp/rag/hybrid data ──
d = { mode: 'auto', reply_text: null, clarification: { message: 'กรุณาระบุ...' }, erp: { answer: rawErpJson } };
assert.strictEqual(sandbox.computeDisplayAnswer(d), 'กรุณาระบุ...');
console.log('PASS: auto mode with no reply_text falls back to clarification, not raw erp.answer');

// ── Test 6: manual (non-auto) developer-tool modes are UNCHANGED — they
// legitimately show the technical/raw answer, since they never claimed
// LINE-OA parity ──
d = { mode: 'erp', erp: { answer: rawErpJson } };
assert.strictEqual(sandbox.computeDisplayAnswer(d), rawErpJson,
  'manual ERP mode must keep showing the raw technical answer — unchanged by this fix');
console.log('PASS: manual ERP mode behavior unchanged (still shows raw answer)');

d = { mode: 'rag', rag: { answer: 'RAG answer text' } };
assert.strictEqual(sandbox.computeDisplayAnswer(d), 'RAG answer text');
console.log('PASS: manual RAG mode behavior unchanged');

d = { mode: 'hybrid', hybrid: { merged_answer: 'merged hybrid text' } };
assert.strictEqual(sandbox.computeDisplayAnswer(d), 'merged hybrid text');
console.log('PASS: manual Hybrid mode behavior unchanged');

console.log('\nAll chat-bubble display-answer tests passed.');
