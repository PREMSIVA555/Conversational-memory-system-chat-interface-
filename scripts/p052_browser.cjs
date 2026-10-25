/* Actual local OIDC -> workspace -> confirmed local support. Synthetic cases
 * are written; no financial data or paid model is modified/called.
 * Intentional acknowledgement/expiry/delay network faults are labelled below.
 */
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const privateModule = path.join(root, '.genesis/local/ux-ui/node_modules/playwright');
const { chromium } = require(fs.existsSync(privateModule) ? privateModule : path.join(root, 'frontend/node_modules/playwright'));
const config = JSON.parse(fs.readFileSync(path.join(root, '.genesis/local/p01/config.json'), 'utf8'));
const report = { scope: 'T05.2 real OIDC/HTTPS UI/API local confirmation with synthetic cases', checks: [], errors: [], paid_model_requests: 0, faults: [] };
const out = path.join(root, '.genesis/evidence/T05.2-browser.json');
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
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1365, height: 900 } });
  page = await context.newPage(); page.setDefaultTimeout(20000); page.setDefaultNavigationTimeout(60000);
  await login(0); await turn('Help');
  let { support, editor } = await openPreview(); await editor.locator('select').first().selectOption('general');
  const preview = await save(editor, 'Synthetic confirmed local support issue');
  const submit = editor.getByRole('button', { name: 'Submit support request', exact: true }); await submit.waitFor();
  await editor.locator('textarea').fill('Unsaved description'); assert(await submit.isDisabled());
  await editor.locator('textarea').fill(preview.payload.summary); assert(await submit.isEnabled());
  check('Submission requires the exact saved preview, with no unsaved changes');
  const requests = [], responses = [];
  await page.route('**/bank-api/support/proposals/*/confirm', async route => {
    requests.push(route.request().postDataJSON()); const r = await route.fetch(); assert.equal(r.status(), 200); responses.push(await r.json());
    if (requests.length === 1) await route.abort('internetdisconnected'); else await route.fulfill({ response: r });
  });
  await submit.click(); await editor.getByText('Submission is uncertain. Check the original submission using the same request.', { exact: true }).waitFor();
  assert(await editor.locator('textarea').isDisabled()); check('Lost acknowledgement after actual commit freezes editing and retains the original operation');
  await page.reload(); await settled(); ({ support, editor } = await openPreview());
  await editor.getByRole('button', { name: 'Check original submission', exact: true }).click();
  await editor.locator('.support-receipt').getByText(responses[0].case_number, { exact: true }).waitFor();
  assert.equal(requests.length, 2); assert.deepEqual(requests[0], requests[1]); assert.deepEqual(responses[0], responses[1]);
  check('Reload recovers exactly the same operation key and immutable creation receipt');
  assert(await editor.getByRole('button', { name: 'Cancel preview', exact: true }).isDisabled());
  assert(await editor.getByText('Creation receipt:', { exact: false }).count()); check('Created receipt is labelled and the consumed preview cannot be edited or cancelled');
  await page.unroute('**/bank-api/support/proposals/*/confirm');
  const duplicate = await api('POST', '/support/proposals/' + preview.id + '/confirm', { ...requests[0], operation_key: crypto.randomUUID() });
  assert.equal(duplicate.status, 409); assert.equal(duplicate.body.detail.case_number, responses[0].case_number);
  const changed = await api('POST', '/support/proposals/' + preview.id + '/confirm', { ...requests[0], payload_hash: 'f'.repeat(64) }); assert.equal(changed.status, 409);
  check('Another key exposes the existing owned case reference; a changed body cannot replay');
  await page.reload(); await settled(); ({ support, editor } = await openPreview());
  await editor.getByRole('button', { name: 'Check original submission', exact: true }).click();
  await editor.locator('.support-receipt').getByText(responses[0].case_number, { exact: true }).waitFor(); check('Known success also survives refresh using the retained original request key');
  await support.getByRole('button', { name: 'Get help with this', exact: true }).click();
  await turn('Reply in Hindi for this conversation'); await page.setViewportSize({ width: 390, height: 844 }); await turn('मदद');
  ({ support, editor } = await openPreview()); await editor.locator('select').first().selectOption('general');
  const hindi = await save(editor, 'सिंथेटिक स्थानीय सहायता अनुरोध');
  await editor.getByRole('button', { name: 'सहायता अनुरोध जमा करें', exact: true }).click();
  await editor.locator('.support-receipt').getByText('सहायता अनुरोध बनाया गया', { exact: true }).waitFor();
  assert(await editor.locator('textarea').evaluate(e => e.getBoundingClientRect().right <= innerWidth));
  await page.screenshot({ path: path.join(root, 'output/playwright/T05.2-hindi-mobile.png') }); check('Hindi mobile explicit submission displays a local creation receipt');
  // Verify both synthetic consumed previews remain readable in this login.
  // No shared financial data, source questions or clocks are changed.
  for (const p of [preview, hindi]) {
    const turns = await api('GET', '/support/proposals/' + p.id); assert.equal(turns.status, 200);
  }
  report.limitations = ['No external support delivery or live provider calls', 'Actual dropped acknowledgement retained; retry uses same persisted request', 'Two synthetic local case records remain with immutable metadata; no direct SQL cleanup/reset', 'Native IME/full accessibility/production load not certified', 'Current owned case status across new login belongs to T05.3'];
  await page.getByRole('button', { name: 'साइन आउट', exact: true }).click();
}
main().catch(e => { report.errors.push({ type: e.name, message: String(e.message).replace(/https?:\/\/\S+/g, '[url]') }); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close(); fs.writeFileSync(out, JSON.stringify(report, null, 2)); console.log(JSON.stringify({ passed: report.checks.length, errors: report.errors.length }));
});
