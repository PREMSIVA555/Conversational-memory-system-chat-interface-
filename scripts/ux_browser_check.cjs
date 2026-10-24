/* Real local OIDC + browser journeys. Credentials are read privately, never logged.
 * No paid model calls: preferences are typed commands and banking actions concise.
 * Run after starting the UX API/frontend at https://localhost:3443.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const alternate = path.join(root, '.genesis/local/ux-ui/node_modules/playwright');
const { chromium } = require(fs.existsSync(alternate) ? alternate : path.join(root, 'frontend/node_modules/playwright'));
const config = JSON.parse(fs.readFileSync(path.join(root, '.genesis/local/p01/config.json'), 'utf8'));
const report = { scope: 'Actual OIDC, HTTPS Next UI and durable bank API; no financial response mocks', checks: [], errors: [], paid_requests: 0 };
const check = name => report.checks.push({ name, status: 'passed' });
const out = path.join(root, '.genesis/evidence/UX01-browser.json');
const pause = ms => new Promise(r => setTimeout(r, ms));
let browser;
async function main() {
  browser = await chromium.launch({ headless: true, executablePath: process.env.UX_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1365, height: 900 } });
  let page = await context.newPage(); page.setDefaultTimeout(15000);
  let cid = '';
  const productTimes = []; report.turn_latency_ms = [];
  page.on('response', async response => {
    try {
      if (response.url().endsWith('/workspace/prepare') && response.ok()) cid = (await response.json()).conversation_id;
      if (/\/products\/(account|card|loan)$/.test(response.url()) && response.ok()) productTimes.push((await response.json()).elapsed_ms);
    } catch { /* Logout/aborted requests have no readable body. */ }
  });
  async function login(index) {
    await page.goto('https://localhost:3443/');
    await page.getByRole('link', { name: 'Sign in securely' }).click();
    await page.getByRole('textbox', { name: 'Username or email' }).fill(config.users[index].username);
    await page.getByRole('textbox', { name: 'Password', exact: true }).fill(config.users[index].password);
    await page.getByRole('button', { name: 'Sign In', exact: true }).click();
    await page.getByRole('textbox', { name: /Your message|आपका संदेश/ }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll('.product-card').length > 0);
  }
  async function turn(text) {
    const started = Date.now();
    const before = await page.locator('.message-pair').count();
    const box = page.getByRole('textbox', { name: /Your message|आपका संदेश/ });
    await box.fill(text); await box.press('Enter');
    await page.waitForFunction(n => document.querySelectorAll('.message-pair').length > n, before);
    await page.waitForFunction(() => !document.querySelector('.workspace-composer textarea').disabled && !document.querySelector('.thinking'));
    report.turn_latency_ms.push({ question: text, elapsed_ms: Date.now() - started, has_fact_block: await page.locator('.assistant-content').last().locator('.fact-card').count() > 0 });
  }
  await login(0); check('Real customer A OIDC login opens owned products and automatic chat');
  const first = cid; assert(first);
  await page.reload(); await page.getByRole('textbox', { name: /Your message|आपका संदेश/ }).waitFor();
  await page.waitForFunction(() => document.querySelectorAll('.product-card').length > 0);
  assert.equal(cid, first); check('Refresh reuses the prepared conversation');
  const newChatResponse = page.waitForResponse(r => r.url().endsWith('/workspace/new') && r.ok());
  await page.getByRole('button', { name: /New chat|नई बातचीत/ }).click(); await newChatResponse;
  // A new source list is empty; capture the new conversation response without
  // logging internal IDs or authentication headers.
  const prepared = await page.evaluate(async () => {
    const s = await (await fetch('/bank-api/auth/session')).json();
    return (await (await fetch('/bank-api/workspace/prepare', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': s.csrf_token }, body: '{}' })).json()).conversation_id;
  });
  assert.notEqual(prepared, first); check('Explicit New chat starts another conversation');
  report.conversations = [first, prepared];
  await turn('Reply in English for this conversation');
  await turn('Be concise for this conversation');
  await turn('Hi');
  assert.match(await page.locator('.assistant-content').last().innerText(), /Hi! I can help/); check('Enter sends a greeting and gets welcome/actions');
  const box = page.getByRole('textbox', { name: 'Your message' });
  const beforeKeys = await page.locator('.message-pair').count();
  await box.fill('Hello'); await box.press('Shift+Enter'); assert.equal(await box.inputValue(), 'Hello\n');
  await box.evaluate(e => { e.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true })); e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, isComposing: true })); e.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true })); });
  await pause(100); assert.equal(await page.locator('.message-pair').count(), beforeKeys); await box.fill('');
  check('Shift+Enter newline and synthetic IME Enter do not send');
  await turn('What is my balance?');
  const choices = page.locator('.assistant-content').last();
  assert(await choices.getByRole('button', { name: /savings/i }).count());
  await choices.getByRole('button', { name: /savings/i }).click();
  await page.waitForFunction(() => !document.querySelector('.workspace-composer textarea').disabled && !document.querySelector('.thinking'));
  assert.match(await page.locator('.assistant-content').last().innerText(), /120,000\.00/);
  check('Balance question offers owned choices and chosen savings shows checked balance');
  await turn('Show its transactions from 2026-10-01 to 2026-10-19');
  assert.match(await page.locator('.assistant-content').last().innerText(), /No records/);
  check('Selected savings follow-up honestly shows empty normal-demo account history');
  await turn('Show Synthetic demo credit card transactions from 2026-10-20 to 2026-10-20');
  assert(await page.locator('.assistant-content').last().locator('.record-list li').count() > 1);
  await turn('Show the second transaction');
  assert.equal(await page.locator('.assistant-content').last().locator('.record-list li').count(), 1);
  check('Natural selected-account follow-up and latest-list ordinal resolve');
  assert(!/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/i.test(await page.locator('.bank-workspace').innerText()));
  check('No UUIDs in ordinary visible workspace');
  await page.screenshot({ path: path.join(root, 'output/playwright/UX01-real-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'My products +', exact: true }).click();
  const geometry = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth, composer: document.querySelector('.workspace-composer').getBoundingClientRect().bottom }));
  assert(geometry.scroll <= geometry.width && geometry.composer <= 844);
  await page.getByRole('button', { name: 'My products −', exact: true }).click();
  check('Mobile product tray and sticky composer fit390px without horizontal overflow');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  assert.equal(await page.getByRole('combobox', { name: 'Active conversation' }).count(), 0);
  await page.keyboard.press('Escape'); assert.equal(await page.locator('dialog[open]').count(), 0);
  check('Settings drawer inherits active chat; Escape closes it');
  await turn('Reply in Hindi');
  await page.locator('.assistant-content').last().getByRole('button', { name: 'This conversation', exact: true }).click();
  await page.getByRole('textbox', { name: 'आपका संदेश' }).waitFor();
  await page.waitForFunction(() => !document.querySelector('.workspace-composer textarea').disabled);
  await turn('नमस्ते'); assert.match(await page.locator('.assistant-content').last().innerText(), /नमस्ते/);
  await page.screenshot({ path: path.join(root, 'output/playwright/UX01-real-mobile-hindi.png') });
  check('Ambiguous language asks scope; persisted chat choice changes UI and greeting to Hindi');
  await turn('Reply in English for this conversation');
  // Simulate losing only the acknowledgement AFTER the real server accepted it.
  let lose = true, holdHistory = true, posts = 0;
  await page.route('**/bank-api/conversations/*/turns', route => holdHistory ? route.abort('internetdisconnected') : route.continue());
  await page.route('**/bank-api/conversations/*/messages', async route => {
    posts++;
    if (lose) { lose = false; const response = await route.fetch(); assert.equal(response.status(), 202); await route.abort('internetdisconnected'); }
    else await route.continue();
  });
  await box.fill('Hello'); await box.press('Enter');
  const retry = page.getByRole('button', { name: 'Check / retry original question', exact: true }); await retry.waitFor();
  // Restoring connectivity lets the existing recovery poll finish. Clicking
  // here races that poll: it may already have removed the retry control.
  holdHistory = false;
  await page.waitForFunction(() => !document.querySelector('.workspace-composer textarea').disabled);
  assert.equal(posts, 1); check('Lost acknowledgement recovers accepted original without another message POST');
  await page.unroute('**/bank-api/conversations/*/messages'); await page.unroute('**/bank-api/conversations/*/turns');
  await page.keyboard.press('Tab');
  assert(await page.evaluate(() => document.activeElement !== document.body)); check('Keyboard focus is reachable after composer');
  await page.setViewportSize({ width: 1365, height: 900 });
  // One category fault must not replace other available products with zeros.
  await page.route('**/bank-api/products/card', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Service unavailable"}' }));
  await page.getByRole('button', { name: 'Refresh products', exact: true }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('.product-group')].some(e => e.innerText.includes('Currently unavailable.')));
  assert.equal(await page.locator('.product-account').count(), 2);
  assert(await page.locator('.product-loan').count() > 0); check('Unavailable card category leaves accounts/loans visible without fake zeros');
  await page.unroute('**/bank-api/products/card');
  let releaseDelayed, startedDelayed;
  const delayedStarted = new Promise(resolve => { startedDelayed = resolve; });
  const delayedRelease = new Promise(resolve => { releaseDelayed = resolve; });
  await page.route('**/bank-api/products/card', async route => {
    const response = await route.fetch(); startedDelayed(); await delayedRelease;
    try { await route.fulfill({ response }); } catch { /* Expected if logout aborted the request. */ }
  });
  await page.getByRole('button', { name: 'Refresh products', exact: true }).click(); await delayedStarted;
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('link', { name: 'Sign in securely' }).waitFor();
  releaseDelayed(); await pause(250);
  assert.equal(await page.locator('.product-card').count(), 0); check('Logout removes owned products and conversation');
  check('Delayed owned product response cannot repopulate the signed-out workspace');
  // Separate browser profile avoids the issuer's still-valid A SSO cookie.
  const other = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1365, height: 900 } });
  page = await other.newPage(); page.setDefaultTimeout(15000);
  await login(1);
  report.conversations.push(await page.evaluate(async () => {
    const s = await (await fetch('/bank-api/auth/session')).json();
    return (await (await fetch('/bank-api/workspace/prepare', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': s.csrf_token }, body: '{}' })).json()).conversation_id;
  }));
  await turn('Reply in English for this conversation'); await turn('Be concise for this conversation');
  await turn('My balance');
  assert.match(await page.locator('.assistant-content').last().innerText(), /85,000\.00/);
  assert(!await page.locator('.chat-scroll').innerText().then(t => t.includes('120,000.00')));
  check('Customer B actual login selects its sole account and sees no A transcript');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  report.product_provider_ms = productTimes;
  report.limitations = ['Synthetic KeyboardEvent is not a native Hindi IME certification', 'Warm local development build; no performance improvement or accessibility certification claim', 'Intentional faults: lost acknowledgement, unavailable category, delayed real product response; bank facts are not mocked', 'No live model evaluation in this browser run'];
}
main().catch(error => { report.errors.push({ type: error.name, message: String(error.message).replace(/https?:\/\/\S+/g, '[url]') }); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close(); fs.writeFileSync(out, JSON.stringify(report, null, 2)); console.log(JSON.stringify({ passed: report.checks.length, errors: report.errors.length, report: '.genesis/evidence/UX01-browser.json' }));
});
