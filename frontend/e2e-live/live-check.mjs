// Live browser check: the real dashboard on the real API (no mocks).
// Run: node e2e-live/live-check.mjs  (with MailMind's API on 8000 and UI on 5173)
import { chromium } from '@playwright/test';

const UI = 'http://127.0.0.1:5173/';
const TABS = ['Inbox', 'Action Center', 'Usage', 'Money', 'Ask', 'Follow-ups'];
const results = [];
const check = (name, ok, detail = '') => { results.push(ok); console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? `  [${detail}]` : '')); };

const browser = await chromium.launch();
for (const [label, viewport] of [['desktop', { width: 1440, height: 900 }], ['phone', { width: 375, height: 812 }]]) {
  const page = await browser.newPage({ viewport });
  const consoleErrors = [];
  const failed = [];
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text().slice(0, 160)); });
  page.on('pageerror', error => consoleErrors.push('pageerror: ' + String(error).slice(0, 160)));
  const clientErrors = [];
  page.on('response', response => {
    const where = response.status() + ' ' + new URL(response.url()).pathname;
    if (response.status() >= 500) failed.push(where);
    else if (response.status() >= 400) clientErrors.push(where);
  });
  await page.goto(UI);
  await page.getByRole('button', { name: 'Account options' }).waitFor({ timeout: 30000 });
  const started = Date.now();
  await page.locator('.email-card').first().waitFor({ timeout: 30000 });
  check(`${label}: inbox loads real mail`, true, `${Date.now() - started} ms to first card`);
  const kpis = await page.locator('.stat h2').allTextContents();
  check(`${label}: overview shows the new KPIs`, ['Risky emails', 'Spent this month'].every(name => kpis.includes(name)), kpis.join(', '));
  check(`${label}: the daily brief card is on the inbox`, await page.getByRole('region', { name: 'Daily brief' }).isVisible());
  const badges = await page.locator('.risk-panel, .risk-clear').count();
  check(`${label}: email cards carry scam checks`, badges > 0, `${badges} on this page`);
  for (const tab of TABS) {
    await page.getByRole('tab', { name: tab, exact: true }).click();
    await page.waitForTimeout(tab === 'Follow-ups' ? 4000 : 1500);
    const panel = page.locator('#panel-' + tab.toLowerCase().replace(/ /g, '-').replace('action-center', 'actions'));
    const visible = await panel.isVisible();
    const text = (await panel.innerText().catch(() => '')).length;
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(`${label}: ${tab} tab renders`, visible && text > 20, `${text} characters`);
    check(`${label}: ${tab} has no horizontal overflow`, overflow <= 0, `${overflow}px`);
  }
  // The Money tab's month navigation and the Usage security report.
  await page.getByRole('tab', { name: 'Money', exact: true }).click();
  await page.getByRole('button', { name: 'Previous month' }).click();
  await page.waitForTimeout(1500);
  check(`${label}: Money month navigation works`, await page.locator('#panel-money').getByText(/Transactions in/).isVisible());
  await page.getByRole('tab', { name: 'Usage', exact: true }).click();
  check(`${label}: Usage shows the red-team report`, await page.getByRole('region', { name: 'Security report' }).isVisible());
  // The browser logs every 4xx as "Failed to load resource" even when the app
  // asked on purpose. Only these are expected; anything else fails the check.
  const EXPECTED = new Set(['401 /session', '404 /mailmind-supervisor.json']);
  const unexpected = [...new Set(clientErrors)].filter(item => !EXPECTED.has(item));
  console.log(`   ${label} 4xx responses seen: ${[...new Set(clientErrors)].join(', ') || 'none'}`);
  const otherErrors = consoleErrors.filter(text => !text.startsWith('Failed to load resource'));
  check(`${label}: no unexpected 4xx responses`, unexpected.length === 0, unexpected.join(', '));
  check(`${label}: no JavaScript or console errors`, otherErrors.length === 0, otherErrors.slice(0, 3).join(' | '));
  check(`${label}: no server errors while browsing`, failed.length === 0, failed.slice(0, 5).join(', '));
  await page.screenshot({ path: `e2e-live/${label}.png`, fullPage: false });
  await page.close();
}
await browser.close();
const passed = results.filter(Boolean).length;
console.log(`\n${passed}/${results.length} live browser checks passed`);
process.exit(passed === results.length ? 0 : 1);
