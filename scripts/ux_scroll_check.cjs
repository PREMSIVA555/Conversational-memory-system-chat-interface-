/* Browser regression for scroll intent. Synthetic HTTP fixtures exercise the
 * actual Next/React workspace, without authentication, bank writes or models.
 * Run against the local review UI: node scripts/ux_scroll_check.cjs
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const { chromium } = require(path.join(root, '.genesis/local/ux-ui/node_modules/playwright'));
const report = { scope: 'Actual workspace UI with synthetic HTTP fixtures; not a bank/auth integration test', checks: [], errors: [], live_model_calls: 0 };
let browser;
async function main() {
  browser = await chromium.launch({ headless: true, executablePath: process.env.UX_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1365, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15000); page.setDefaultNavigationTimeout(60000);
  let polls = 0, posts = 0;
  const turns = [];
  function append(text = 'Hello from an incoming greeting') {
    const id = turns.length + 1;
    const turn = { turn_id: `scroll-turn-${id}`, client_turn_id: `scroll-client-${id}`, accepted_at: new Date(Date.UTC(2026, 9, 24, 0, 0, id)).toISOString(), status: 'completed', user_text: text,
      result: { text: `Hi! I can help you find your accounts, cards and loans. Greeting ${id}.`, status: 'answered', blocks: [], actions: [] } };
    turns.push(turn); return turn;
  }
  for (let i = 0; i < 24; i++) append(`Hello ${i + 1}`);
  await page.route('**/bank-api/**', async route => {
    const request = route.request(); const url = new URL(request.url()); let body;
    if (url.pathname.endsWith('/auth/session')) body = { customer_id: 'scroll-test-customer', csrf_token: 'synthetic-scroll-csrf', expires_at: 9999999999 };
    else if (url.pathname.endsWith('/workspace/prepare')) body = { conversation_id: 'scroll-test-conversation' };
    else if (url.pathname.endsWith('/preferences')) body = { effective: { preferred_response_language: { value: 'en', scope: 'default', status: 'default' }, preferred_response_format: { value: 'concise', scope: 'default', status: 'default' } }, projections: [], proposals: [] };
    else if (/\/products\/(account|card|loan)$/.test(url.pathname)) body = { status: 'available', items: [], complete: true, next_cursor: null };
    else if (url.pathname.endsWith('/preference-messages')) body = { items: [] };
    else if (url.pathname.endsWith('/turns')) { polls++; body = { items: turns }; }
    else if (url.pathname.endsWith('/messages') && request.method() === 'POST') {
      posts++; const input = request.postDataJSON(); const turn = append(input.text); turn.client_turn_id = input.client_turn_id;
      const completed = turn.result; turn.result = null; turn.status = 'running';
      setTimeout(() => { turn.result = completed; turn.status = 'completed'; }, 2300);
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ turn_id: turn.turn_id }) }); return;
    } else throw new Error(`Unexpected fixture route: ${url.pathname}`);
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });
  const scroll = () => page.locator('.chat-scroll');
  const metrics = () => scroll().evaluate(e => ({ top: e.scrollTop, gap: e.scrollHeight - e.clientHeight - e.scrollTop }));
  const check = name => report.checks.push({ name, status: 'passed' });
  async function waitPolls(n = 3) {
    const target = polls + n, deadline = Date.now() + 15000;
    while (polls < target && Date.now() < deadline) await page.waitForTimeout(100);
    assert(polls >= target, 'History refreshes should arrive'); await page.waitForTimeout(100);
  }
  async function readEarlier() {
    await scroll().hover(); await page.mouse.wheel(0, -600); await page.waitForTimeout(150);
    const m = await metrics(); assert(m.gap > 200, 'Wheel should move away from latest messages'); return m.top;
  }
  async function atBottom() { await page.waitForFunction(() => { const e = document.querySelector('.chat-scroll'); return e && e.scrollHeight - e.clientHeight - e.scrollTop < 3; }); }
  await page.goto('https://localhost:3443/');
  await page.locator('.message-pair').nth(23).waitFor(); await atBottom(); check('Initial history opens at latest messages');
  const before = await readEarlier(); await waitPolls();
  assert(Math.abs((await metrics()).top - before) < 3); check('Reading position survives three unchanged history refreshes');
  const incoming = append(); await page.getByText(incoming.user_text, { exact: true }).waitFor();
  assert(Math.abs((await metrics()).top - before) < 3); check('A new incoming turn does not interrupt reading earlier messages');
  incoming.result.text += ' Additional checked greeting detail.'; await waitPolls(1);
  assert(Math.abs((await metrics()).top - before) < 3); check('An updated reply does not pull a reader to the bottom');
  await scroll().evaluate(e => { e.scrollTop = e.scrollHeight; }); await page.waitForTimeout(100);
  const latest = append('Hello while following latest messages'); await page.getByText(latest.user_text, { exact: true }).waitFor(); await atBottom();
  check('Returning to bottom resumes following new messages');
  await readEarlier(); const box = page.getByRole('textbox', { name: 'Your message' });
  await box.fill('Hello from my own new question'); await box.press('Enter');
  await page.getByText('Hello from my own new question', { exact: true }).waitFor(); await atBottom(); assert.equal(posts, 1);
  check('Sending while reading history reveals the newest turn exactly once');
  await page.waitForFunction(() => !document.querySelector('.thinking')); await atBottom();
  check('A delayed reply remains visible after replacing its progress indicator');
  await page.setViewportSize({ width: 390, height: 844 });
  await scroll().evaluate(e => { e.scrollTop = e.scrollHeight; }); await atBottom();
  const mobileTop = await readEarlier(); await waitPolls();
  assert(Math.abs((await metrics()).top - mobileTop) < 3); check('Mobile reading position survives history refreshes');
  const mobileIncoming = append('Hello arriving while reading on mobile'); await page.getByText(mobileIncoming.user_text, { exact: true }).waitFor();
  assert(Math.abs((await metrics()).top - mobileTop) < 3); check('Mobile incoming messages preserve the reading position');
  report.history_refreshes_observed = polls;
}
main().catch(e => { report.errors.push({ type: e.name, message: e.message }); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  fs.mkdirSync(path.join(root, '.genesis/evidence'), { recursive: true });
  fs.writeFileSync(path.join(root, '.genesis/evidence/UX01-scroll.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: report.checks.length, errors: report.errors.length }));
});
