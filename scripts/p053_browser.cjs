/* Actual local OIDC -> workspace -> confirmed local support. Synthetic cases
 * are written; no financial data or paid model is modified/called.
 * Intentional acknowledgement/expiry/delay network faults are labelled below.
 */
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const privateModule = path.join(root, '.genesis/local/ux-ui/node_modules/playwright');
const { chromium } = require(fs.existsSync(privateModule) ? privateModule : path.join(root, 'frontend/node_modules/playwright'));
const config = JSON.parse(fs.readFileSync(path.join(root, '.genesis/local/p01/config.json'), 'utf8'));
const report = { scope: 'T05.3 actual OIDC/HTTPS reconciliation and owned-case continuity', checks: [], errors: [], paid_model_requests: 0, faults: [] };
const laterOnly = process.argv.includes('--later-only');
const out = path.join(root, `.genesis/evidence/T05.3-browser${laterOnly ? '-later' : ''}.json`);
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
  await Promise.race([page.getByRole('textbox', { name: 'Password', exact: true }).waitFor(), page.getByRole('textbox', { name: /Your message|आपका संदेश/ }).waitFor()]);
  if (await page.getByRole('textbox', { name: 'Password', exact: true }).count()) {
    const username = page.getByRole('textbox', { name: 'Username or email' });
    if (await username.count() && await username.isEditable()) await username.fill(config.users[index].username);
    await page.getByRole('textbox', { name: 'Password', exact: true }).fill(config.users[index].password);
    await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  }
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
async function freshPreview(text) {
  await turn('Help'); const result = await openPreview();
  await result.editor.locator('select').first().selectOption('general');
  return { ...result, preview: await save(result.editor, text) };
}
async function clearSubmissionStorage() {
  await page.evaluate(() => { for (const key of Object.keys(sessionStorage)) if (key.startsWith('bank-support-confirm:')) sessionStorage.removeItem(key); });
}
async function main() {
  browser = await chromium.launch({ headless: true, executablePath: process.env.UX_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1365, height: 900 } });
  page = await context.newPage(); page.setDefaultTimeout(30000); page.setDefaultNavigationTimeout(90000);
  await login(0);
  if (laterOnly) {
    const { preview } = await freshPreview('Synthetic T05.3 next-login pending preview');
    const cases = await api('GET', '/support/cases'); assert(cases.body.items.length);
    await laterLogin(cases.body.items[0], preview.id); return;
  }
  await page.getByRole('region', { name: 'My support requests', exact: true }).waitFor();
  check('Authenticated login loads the separate owned support request panel');
  let { editor, preview } = await freshPreview('Synthetic T05.3 before acceptance network loss');
  const requests = []; let drop = 'before';
  await page.route('**/bank-api/support/proposals/*/confirm', async route => {
    requests.push(route.request().postDataJSON());
    if (drop === 'before') return route.abort('internetdisconnected');
    const response = await route.fetch(); assert.equal(response.status(), 200);
    if (drop === 'after') return route.abort('internetdisconnected');
    return route.fulfill({ response });
  });
  await editor.getByRole('button', { name: 'Submit support request', exact: true }).click();
  await editor.getByText('Submission outcome is unknown. Check the original submission before retrying.', { exact: true }).waitFor();
  assert(await editor.getByRole('button', { name: 'Retry original submission', exact: true }).isDisabled());
  await editor.getByRole('button', { name: 'Check submission status', exact: true }).click();
  await editor.getByText('No committed submission is recorded yet. You can check again or explicitly retry the original request.', { exact: true }).waitFor();
  assert.equal(requests.length, 1); check('Loss before acceptance stays unknown until pure read reconciliation; reads do not submit');
  await clearSubmissionStorage(); await page.reload(); await settled(); ({ editor } = await openPreview());
  drop = 'none'; await editor.getByRole('button', { name: 'Submit support request', exact: true }).click();
  await editor.getByText('Support request recorded', { exact: true }).waitFor();
  assert.deepEqual(requests[1], requests[0]);
  const first = await api('GET', '/support/proposals/' + preview.id + '/outcome');
  assert.equal(first.body.outcome, 'committed'); assert.equal(first.body.request.operation_key, requests[0].operation_key);
  await editor.locator('.support-current').getByText('Current status: Received', { exact: true }).waitFor();
  check('Refresh plus lost browser operation storage preserves the prepared identity and creates one case');
  await page.unroute('**/bank-api/support/proposals/*/confirm');

  ({ editor, preview } = await freshPreview('Synthetic T05.3 after commit acknowledgement loss'));
  const lost = []; await page.route('**/bank-api/support/proposals/*/confirm', async route => {
    lost.push(route.request().postDataJSON()); const response = await route.fetch(); assert.equal(response.status(), 200); await route.abort('internetdisconnected');
  });
  await editor.getByRole('button', { name: 'Submit support request', exact: true }).click();
  await editor.getByText('Submission outcome is unknown. Check the original submission before retrying.', { exact: true }).waitFor();
  await editor.getByRole('button', { name: 'Check submission status', exact: true }).click();
  await editor.getByText('Support request recorded', { exact: true }).waitFor();
  const committed = await api('GET', '/support/proposals/' + preview.id + '/outcome');
  assert.equal(committed.body.outcome, 'committed'); assert.equal(lost.length, 1);
  assert.equal(committed.body.request.operation_key, lost[0].operation_key);
  check('Lost committed acknowledgement resolves original case by read without another confirmation');
  await clearSubmissionStorage(); await page.reload(); await settled(); ({ editor } = await openPreview());
  await editor.locator('.support-receipt').getByText(committed.body.current_case.case_number, { exact: true }).waitFor();
  assert.equal(lost.length, 1); check('Committed outcome and original intent recover after storage loss without submitting');
  await page.unroute('**/bank-api/support/proposals/*/confirm');

  ({ editor, preview } = await freshPreview('Synthetic T05.3 status lookup unavailable after known success'));
  await editor.getByRole('button', { name: 'Submit support request', exact: true }).waitFor();
  await page.route('**/bank-api/support/proposals/*/reconcile', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Intentional current-status outage"}' }));
  await editor.getByRole('button', { name: 'Submit support request', exact: true }).click();
  await editor.getByText('Your request is recorded, but its current status could not be refreshed.', { exact: true }).waitFor();
  await editor.getByText('Support request recorded', { exact: true }).waitFor();
  assert.equal(await editor.getByRole('button', { name: 'Retry original submission', exact: true }).count(), 0);
  check('Known creation receipt survives a current-status outage with no false current status or retry');
  await page.unroute('**/bank-api/support/proposals/*/reconcile');
  await editor.getByRole('button', { name: 'Check submission status', exact: true }).click();
  await editor.locator('.support-current').getByText('Current status: Received', { exact: true }).waitFor();
  check('Explicit refresh restores checked current status separately from creation receipt');

  await laterLogin(committed.body.current_case, preview.id);
  report.faults = ['Browser abort before confirm reaches API', 'Browser abort after real committed response', 'Browser submission storage removed', 'Current-status response 503 after known creation'];
  report.retained_synthetic_cases = 3;
}
async function laterLogin(originalCase, previewId) {
  await page.getByRole('button', { name: /Sign out|Log out/, exact: true }).click();
  await page.getByRole('link', { name: 'Sign in securely' }).waitFor();
  await login(0);
  const cases = page.getByRole('region', { name: 'My support requests', exact: true });
  await cases.getByRole('button', { name: new RegExp(originalCase.case_number) }).click();
  await cases.locator('.support-current').getByText('Current status: Received', { exact: true }).waitFor();
  assert.equal((await api('GET', '/support/proposals/' + previewId + '/outcome')).status, 404);
  check('Later login reads fresh owned case status while old session preview reconciliation is denied');
  await turn('Reply in Hindi for this conversation'); await page.setViewportSize({ width: 390, height: 844 });
  const tray = page.getByRole('button', { name: /मेरे उत्पाद/ });
  if (await tray.count() && await tray.getAttribute('aria-expanded') === 'false') await tray.click();
  const hindi = page.getByRole('region', { name: 'मेरे सहायता अनुरोध', exact: true });
  await hindi.getByRole('button', { name: new RegExp(originalCase.case_number) }).click();
  await hindi.locator('.support-current').getByText('वर्तमान स्थिति: प्राप्त', { exact: true }).waitFor();
  assert(await hindi.evaluate(e => e.getBoundingClientRect().right <= innerWidth + 1));
  fs.mkdirSync(path.join(root, 'output/playwright'), { recursive: true });
  await page.screenshot({ path: path.join(root, 'output/playwright/T05.3-hindi-mobile.png') });
  check('Hindi mobile owned-case panel exposes current status with contained layout');
}
main().catch(async e => { report.errors.push(String(e.stack || e)); if (page) { report.failed_page_text = await page.locator('body').innerText().catch(() => 'unavailable'); } process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close(); fs.writeFileSync(out, JSON.stringify(report, null, 2)); console.log(JSON.stringify({ passed: report.checks.length, errors: report.errors.length }));
});
