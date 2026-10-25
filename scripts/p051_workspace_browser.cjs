/* Actual local OIDC -> workspace -> support persistence. Only synthetic drafts
 * are written; no financial data, case writer or paid model is exercised.
 * Intentional acknowledgement/expiry/delay network faults are labelled below.
 */
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const privateModule = path.join(root, '.genesis/local/ux-ui/node_modules/playwright');
const { chromium } = require(fs.existsSync(privateModule) ? privateModule : path.join(root, 'frontend/node_modules/playwright'));
const config = JSON.parse(fs.readFileSync(path.join(root, '.genesis/local/p01/config.json'), 'utf8'));
const report = { scope: 'Real OIDC/HTTPS UI/API persistence with synthetic support drafts', checks: [], errors: [], paid_model_requests: 0, faults: [] };
const out = path.join(root, '.genesis/evidence/T05.1-workspace-browser.json');
let browser, page;
const check = name => report.checks.push({ name, status: 'passed' });
async function settled() { await page.waitForFunction(() => { const e = document.querySelector('.workspace-composer textarea'); return e && !e.disabled && !document.querySelector('.thinking'); }); }
async function turn(text) {
  await settled(); const count = await page.locator('.message-pair').count();
  const box = page.getByRole('textbox', { name: /Your message|आपका संदेश/ }); await box.fill(text); await box.press('Enter');
  await page.waitForFunction(n => document.querySelectorAll('.message-pair').length > n, count); await settled();
}
async function login(index) {
  await page.goto('https://localhost:3443/');
  await page.getByRole('link', { name: 'Sign in securely' }).click();
  await page.getByRole('textbox', { name: 'Username or email' }).fill(config.users[index].username);
  await page.getByRole('textbox', { name: 'Password', exact: true }).fill(config.users[index].password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  await page.getByRole('textbox', { name: /Your message|आपका संदेश/ }).waitFor();
  await turn('Reply in English for this conversation'); await turn('Be concise for this conversation');
}
async function openPreview() {
  const support = page.locator('.message-pair').last().locator('.support-preview');
  await support.getByRole('button', { name: /Get help with this|इस बारे में सहायता लें/ }).click();
  const editor = support.locator('.support-editor'); await editor.waitFor();
  await editor.getByRole('button', { name: /Refresh saved preview|सेव किया गया पूर्वावलोकन रीफ़्रेश करें/ }).waitFor();
  return { support, editor };
}
async function save(editor, text, revised = false) {
  await editor.locator('textarea').fill(text);
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && /\/support\/proposals(?:\/[^/]+\/edit)?$/.test(new URL(r.url()).pathname)),
    editor.getByRole('button', { name: revised ? /Save revised preview|संशोधित पूर्वावलोकन सेव करें/ : /Save preview|पूर्वावलोकन सेव करें/, exact: true }).click(),
  ]);
  assert.equal(response.status(), 201); const body = await response.json();
  await page.waitForFunction(text => [...document.querySelectorAll('.support-summary')].some(e => e.textContent === text), body.payload.summary);
  return body;
}
async function cancel(editor) {
  await editor.getByRole('button', { name: /Cancel preview|पूर्वावलोकन रद्द करें/, exact: true }).click();
  await editor.locator('.support-saved').getByText(/Cancelled|रद्द/, { exact: true }).waitFor();
}
async function api(method, endpoint, body) {
  return page.evaluate(async ({ method, endpoint, body }) => {
    const s = await (await fetch('/bank-api/auth/session')).json();
    const r = await fetch('/bank-api' + endpoint, { method, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': s.csrf_token }, ...(body ? { body: JSON.stringify(body) } : {}) });
    return { status: r.status, body: await r.json() };
  }, { method, endpoint, body });
}
async function main() {
  browser = await chromium.launch({ headless: true, executablePath: process.env.UX_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  let context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1365, height: 900 } });
  page = await context.newPage(); page.setDefaultTimeout(20000); page.setDefaultNavigationTimeout(60000);
  await login(0);
  await page.locator('.product-account').first().getByRole('button', { name: 'Balance', exact: true }).click(); await settled();
  let { support, editor } = await openPreview();
  assert.equal(await editor.locator('select').first().inputValue(), 'account');
  await page.waitForFunction(() => { const e = document.querySelector('.support-editor select[required]'); return e && !e.disabled && e.value; });
  assert(!await editor.getByRole('textbox', { name: /Full resource reference/ }).count());
  check('Owned account is preselected from the checked answer without a typed UUID');
  const first = await save(editor, '  Synthetic workspace issue\nKeep <b>this markup</b> as text.  ');
  assert.equal(first.version, 1); assert.equal(first.payload.summary, 'Synthetic workspace issue\nKeep <b>this markup</b> as text.');
  assert(first.resource_label && !first.resource_label.includes(first.payload.resource_id));
  assert.equal(await editor.locator('.support-summary b').count(), 0); check('Exact normalized preview persists and markup stays plain text');
  const second = await save(editor, 'Synthetic revised issue', true); assert.equal(second.version, 2); assert.notEqual(second.id, first.id);
  assert.equal((await api('GET', '/support/proposals/' + first.id)).body.status, 'cancelled'); check('Edit creates version2 and cancels the original saved row');
  const confirmed = await api('POST', '/support/proposals/' + second.id + '/confirm', { version: second.version, payload_hash: second.payload_hash });
  assert.equal(confirmed.status, 503); assert.equal((await api('GET', '/support/proposals/' + second.id)).body.status, 'pending');
  assert(await editor.getByText('Save and review your description. Case submission is not available yet.', { exact: true }).count());
  check('Valid confirm remains unavailable and does not consume the draft');
  await page.reload(); await settled(); ({ support, editor } = await openPreview());
  await editor.locator('.support-summary').getByText('Synthetic revised issue', { exact: true }).waitFor();
  assert.match(await editor.locator('.support-saved').innerText(), /Version 2/); check('Refresh and automatic chat reuse restore the exact persisted preview');
  await cancel(editor); assert(await editor.locator('textarea').isDisabled()); check('Cancellation is persisted and prevents further edits');
  await support.getByRole('button', { name: 'Get help with this', exact: true }).click();
  await turn('Help'); ({ support, editor } = await openPreview());
  await editor.locator('select').first().selectOption('card');
  await page.waitForFunction(() => document.querySelectorAll('.support-product-choice option').length >= 3);
  const cardOptions = await editor.locator('select[required] option').allTextContents(); assert(cardOptions.some(t => t.includes('credit') && t.includes('1111')));
  const cardOption = editor.locator('select[required] option').filter({ hasText: /credit/ }).first(); const cardId = await cardOption.getAttribute('value');
  await editor.locator('select[required]').selectOption(cardId); const card = await save(editor, 'Synthetic card support issue');
  assert.equal(card.payload.topic, 'card'); assert.equal(card.payload.resource_id, cardId); assert.match(card.resource_label, /credit.*1111/);
  check('Friendly card chooser distinguishes actual kind/suffix and persists the selected owned card');
  // Server-side expiry is covered in guarded SQL/HTTP tests. This explicit UI
  // fault verifies rendering of an expired read without altering demo clocks.
  const latestPath = '**/bank-api/support/proposals/by-turn/' + card.source_turn_id;
  await page.route(latestPath, async route => { const r = await route.fetch(); await route.fulfill({ response: r, json: { ...(await r.json()), status: 'expired' } }); });
  await editor.getByRole('button', { name: 'Refresh saved preview', exact: true }).click();
  await editor.locator('.support-saved').getByText('Expired', { exact: true }).waitFor(); assert(await editor.locator('textarea').isDisabled());
  check('Expired server status freezes editing (labelled UI response fault)'); report.faults.push('Injected expired GET response; actual DB expiry is tested separately in guarded worlds');
  await page.unroute(latestPath); await api('POST', '/support/proposals/' + card.id + '/cancel', { version: card.version, payload_hash: card.payload_hash });
  await support.getByRole('button', { name: 'Get help with this', exact: true }).click();
  await turn('Help'); ({ support, editor } = await openPreview()); await editor.locator('select').first().selectOption('general');
  let posts = 0, lost = null;
  await page.route('**/bank-api/support/proposals', async route => {
    posts++; const r = await route.fetch(); assert.equal(r.status(), 201); lost = await r.json(); await route.abort('internetdisconnected');
  });
  await editor.locator('textarea').fill('Synthetic acknowledgement recovery'); await editor.getByRole('button', { name: 'Save preview', exact: true }).click();
  await editor.getByText('The result is uncertain. Refresh the saved preview before retrying.', { exact: true }).waitFor();
  assert(await editor.locator('textarea').isDisabled());
  await page.unroute('**/bank-api/support/proposals'); await editor.getByRole('button', { name: 'Refresh saved preview', exact: true }).click();
  await editor.locator('.support-summary').getByText('Synthetic acknowledgement recovery', { exact: true }).waitFor(); assert.equal(posts, 1); assert.equal(lost.version, 1);
  check('Lost save acknowledgement recovers the original version without another create POST'); report.faults.push('Dropped acknowledgement only after actual201 persisted');
  await cancel(editor); await support.getByRole('button', { name: 'Get help with this', exact: true }).click();
  // UI-only category faults and pagination use the actual owned product page,
  // split into smaller pages. They do not create financial fixture records.
  const owned = (await api('GET', '/products/account')).body;
  assert(owned.items.length >= 2, 'This pagination journey requires two normal-demo owned accounts');
  await turn('Help'); ({ support, editor } = await openPreview());
  let productMode = 'empty';
  await page.route('**/bank-api/products/account*', async route => {
    const next = new URL(route.request().url()).searchParams.has('cursor');
    const json = productMode === 'unavailable' ? { ...owned, status: 'unavailable', items: [], next_cursor: null }
      : productMode === 'empty' ? { ...owned, items: [], next_cursor: null }
      : { ...owned, items: next ? owned.items.slice(1) : owned.items.slice(0, 1), next_cursor: next ? null : 'ui-split-page' };
    await route.fulfill({ status: 200, contentType: 'application/json', json });
  });
  await editor.locator('select').first().selectOption('general');
  await editor.locator('select').first().selectOption('account');
  await editor.getByText('No products are available in this category.', { exact: true }).waitFor();
  await editor.locator('textarea').fill('Synthetic chooser boundary');
  assert(await editor.getByRole('button', { name: 'Save preview', exact: true }).isDisabled()); check('Empty product category cannot save an unselected account (labelled UI fixture)');
  productMode = 'unavailable'; await editor.locator('select').first().selectOption('general'); await editor.locator('select').first().selectOption('account');
  await editor.getByText('Products are currently unavailable.', { exact: false }).waitFor();
  assert(await editor.getByRole('button', { name: 'Save preview', exact: true }).isDisabled()); check('Unavailable product category stays unavailable and prevents save (labelled UI fixture)');
  productMode = 'pages'; await editor.getByRole('button', { name: 'Retry products', exact: true }).click();
  await editor.getByRole('button', { name: 'Load more products', exact: true }).click();
  await page.waitForFunction(n => document.querySelectorAll('.support-product-choice select option').length === n + 1, owned.items.length);
  await editor.locator('select[required]').selectOption(owned.items[1].id);
  assert(await editor.getByRole('button', { name: 'Save preview', exact: true }).isEnabled()); check('Product pagination appends friendly owned choices and enables selection (split real page fixture)');
  report.faults.push('Injected empty/unavailable category pages and split actual owned account page into two UI pages; no financial data changed');
  await page.unroute('**/bank-api/products/account*'); await support.getByRole('button', { name: 'Get help with this', exact: true }).click();
  await turn('Reply in Hindi for this conversation'); await page.setViewportSize({ width: 390, height: 844 });
  await turn('मदद'); ({ support, editor } = await openPreview()); await editor.locator('select').first().selectOption('general');
  const hindi = await save(editor, 'सिंथेटिक सहायता अनुरोध'); assert.equal(hindi.payload.resource_id, null);
  assert(await editor.getByText('अपनी समस्या सेव करके जाँचें। अभी केस जमा करना उपलब्ध नहीं है।', { exact: true }).count());
  assert(await editor.locator('textarea').evaluate(e => { const r = e.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }));
  await page.screenshot({ path: path.join(root, 'output/playwright/T05.1-workspace-hindi-mobile.png') });
  check('Hindi mobile form saves a General preview with no resource and usable layout');
  assert(!/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/i.test(await page.locator('.bank-workspace').innerText())); check('Ordinary visible support UI contains no UUIDs');
  await cancel(editor); await support.getByRole('button', { name: 'इस बारे में सहायता लें', exact: true }).click();
  await turn('Reply in English for this conversation'); await turn('Help'); ({ support, editor } = await openPreview()); await editor.locator('select').first().selectOption('general');
  let release, started; const delayed = new Promise(r => { release = r; }); const accepted = new Promise(r => { started = r; });
  await page.route('**/bank-api/support/proposals', async route => {
    const response = await route.fetch(); assert.equal(response.status(), 201); started(); await delayed;
    try { await route.fulfill({ response }); } catch { /* Logout intentionally aborts the old component. */ }
  });
  await editor.locator('textarea').fill('Synthetic delayed A response'); await editor.getByRole('button', { name: 'Save preview', exact: true }).click(); await accepted;
  await page.getByRole('button', { name: 'Sign out', exact: true }).click(); await page.getByRole('link', { name: 'Sign in securely' }).waitFor(); release();
  await page.waitForTimeout(250); assert.equal(await page.locator('.support-editor').count(), 0); check('Delayed persisted A response cannot repopulate the signed-out UI');
  context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1365, height: 900 } }); page = await context.newPage(); page.setDefaultTimeout(20000); page.setDefaultNavigationTimeout(60000);
  await login(1); assert(!await page.locator('.bank-workspace').innerText().then(t => t.includes('Synthetic delayed A response')));
  assert.equal((await api('GET', '/support/proposals/' + second.id)).status, 404); check('Customer B login neither displays nor retrieves customer A preview');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  report.limitations = ['No real case submission or financial mutation', 'Expired UI status is an injected GET response; actual clock/erasure/ownership are separately verified in guarded SQL/HTTP worlds', 'Lost/delayed response faults retain actual persisted mutations', 'Native touch/IME and full accessibility certification are not claimed', 'Final delayed A draft remains session-inaccessible and expires normally; no direct SQL cleanup or demo reset'];
}
main().catch(e => { report.errors.push({ type: e.name, message: String(e.message).replace(/https?:\/\/\S+/g, '[url]') }); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close(); fs.writeFileSync(out, JSON.stringify(report, null, 2)); console.log(JSON.stringify({ passed: report.checks.length, errors: report.errors.length }));
});
