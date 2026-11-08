import { expect, test } from '@playwright/test';

/**
 * End-to-end coverage of the flows the product must keep working: read the
 * market, ask a question, inspect a stock, scan patterns, run experiments,
 * keep a watchlist and expose the agent surface. Runs against SIM so it is
 * deterministic in time; `pulse.py e2e` points the API at a scratch data dir.
 */

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => window.localStorage.removeItem('pulse.workspace.v3'));
});

test('overview shows mood, ladder, seal clock and tape with provenance', async ({ page }) => {
  await page.goto('/#/overview');
  await expect(page.getByRole('meter', { name: '情绪温度' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '连板天梯' })).toBeVisible();
  await expect(page.locator('.rung').first()).toBeVisible();
  await expect(page.getByRole('heading', { name: '封板时钟' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '盘口异动' })).toBeVisible();
  await expect(page.locator('.mode')).toContainText(/SIM|LIVE/);
});

test('a question becomes removable conditions and narrows the result', async ({ page }) => {
  await page.goto('/#/screener');
  const count = page.locator('.results-count strong');
  await expect(count).not.toHaveText('—');
  const before = Number(await count.textContent());

  await page.getByLabel('用一句话描述选股条件').fill('2连板以上 换手低于20%');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: /移除条件 连板/ })).toBeVisible();
  await expect.poll(async () => Number(await count.textContent())).toBeLessThan(before);

  await page.getByRole('button', { name: '清空全部' }).click();
  await expect.poll(async () => Number(await count.textContent())).toBe(before);
});

test('keyboard opens the stock sheet with a K-line and Esc closes it', async ({ page }) => {
  await page.goto('/#/screener');
  const grid = page.locator('.grid-body');
  await expect(page.locator('.grid-row').first()).toBeVisible();
  await grid.focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  const sheet = page.getByRole('dialog');
  await expect(sheet).toBeVisible();
  await expect(page).toHaveURL(/\?s=\d{6}/);
  await expect(sheet.locator('.kchart canvas').first()).toBeVisible({ timeout: 20_000 });
  await expect(sheet.getByText('因子拆解')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(sheet).toBeHidden();
  await expect(page).not.toHaveURL(/\?s=/);
});

test('command palette searches by code and opens the stock', async ({ page }) => {
  await page.goto('/#/overview');
  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: '命令面板' });
  await expect(palette).toBeVisible();
  await page.getByRole('combobox').fill('0020');
  await expect(palette.getByRole('option').first()).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog').filter({ hasNot: page.getByRole('combobox') })).toBeVisible();
});

test('pattern radar lists rule cards and their hits', async ({ page }) => {
  await page.goto('/#/patterns');
  await expect(page.locator('.pattern-card')).toHaveCount(12, { timeout: 20_000 });
  await page.getByRole('button', { name: /^均线多头/ }).click();
  await expect(page.getByRole('heading', { name: '均线多头' }).last()).toBeVisible();
});

test('lab runs a backtest tear sheet and a factor IC study', async ({ page }) => {
  await page.goto('/#/lab');
  await page.getByRole('button', { name: '运行回测' }).click();
  await expect(page.locator('.kpi').first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('月度收益')).toBeVisible();

  await page.getByRole('radio', { name: '因子检验' }).click();
  await expect(page.locator('.fstat').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('heading', { name: '分组前瞻收益' })).toBeVisible();
});

test('watchlist round trip from suggestions', async ({ page }) => {
  await page.goto('/#/watchlist');
  await page.evaluate(() => fetch('/api/v1/watchlist', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ codes: [] }) }));
  await page.reload();
  const suggestion = page.locator('.suggest-card').first();
  await expect(suggestion).toBeVisible();
  await suggestion.click();
  await expect(page.locator('.watch-card').first()).toBeVisible();
  await page.locator('.watch-card').first().hover();
  await page.locator('.watch-remove').first().click();
  await expect(page.locator('.suggest-card').first()).toBeVisible();
});

test('sources dialog declares providers and field coverage', async ({ page }) => {
  await page.goto('/#/overview');
  await page.locator('.mode').click();
  const dialog = page.getByRole('dialog', { name: '数据来源' });
  await expect(dialog.locator('.providers li').first()).toBeVisible();
  await expect(dialog.locator('.coverage li').first()).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});

test('deep links and digit shortcuts switch views', async ({ page }) => {
  await page.goto('/#/themes');
  await expect(page.locator('.tile-theme').first()).toBeVisible();
  await page.locator('body').press('7');
  await expect(page).toHaveURL(/#\/patterns/);
  await page.locator('body').press('1');
  await expect(page).toHaveURL(/#\/overview/);
});

test('daily picks show the regime, a ranked board, weights and a track record', async ({ page }) => {
  await page.goto('/#/picks');
  await expect(page.locator('.regime-banner')).toBeVisible({ timeout: 90_000 });
  await expect(page.locator('.history-chip')).toContainText(/合成历史|真实历史/);
  await expect(page.getByRole('heading', { name: '参与加权的战法' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '以史为鉴' })).toBeVisible();
  const board = page.locator('.podium-card, .empty');
  await expect(board.first()).toBeVisible();
  for (const label of ['短线', '波段', '长线']) {
    await page.getByRole('radio', { name: new RegExp(`^${label}`) }).click();
    await expect(page.locator('.podium-card, .empty').first()).toBeVisible();
  }
});

test('playbook library lists backtested playbooks and opens a tear sheet', async ({ page }) => {
  await page.goto('/#/playbooks');
  const items = page.locator('.pb-item');
  await expect(items.first()).toBeVisible({ timeout: 90_000 });
  expect(await items.count()).toBeGreaterThanOrEqual(15);
  await items.nth(1).click();
  await expect(page).toHaveURL(/#\/playbooks\?p=[a-z0-9_]+/);
  const detail = page.locator('.pb-detail');
  await expect(detail.getByRole('heading', { level: 2 })).toBeVisible();
  await expect(detail.getByText('自定义回测')).toBeVisible();
  // The walk-forward rotation and the sell-rule comparison come with every library visit.
  await expect(page.getByRole('heading', { name: '战法轮换' })).toBeVisible();
  await expect(page.locator('.rotation-verdict')).toContainText(/轮换/, { timeout: 90_000 });
  await expect(detail.locator('.exit-table tbody tr').first()).toBeVisible({ timeout: 90_000 });
  await expect(detail.locator('.exit-plan li').last()).toContainText('以上都没触发');
});

test('themes page carries the crowd snapshot', async ({ page }) => {
  await page.goto('/#/themes');
  const crowd = page.locator('.crowd-panel');
  await expect(crowd.getByRole('heading', { name: '抱团与联动' })).toBeVisible({ timeout: 60_000 });
  await expect(crowd.getByText('行业扩散')).toBeVisible();
  await expect(crowd.locator('.assumptions li').first()).toBeVisible();
});

test('regime view draws the timeline and the playbook-by-regime matrix', async ({ page }) => {
  await page.goto('/#/regime');
  await expect(page.locator('.timeline svg')).toBeVisible({ timeout: 60_000 });
  await expect(page.locator('.regime-rules li').first()).toBeVisible();
  await expect(page.locator('.fit-matrix tbody tr').first()).toBeVisible({ timeout: 90_000 });
});

test('stock sheet shows the analog forecast fan', async ({ page }) => {
  await page.goto('/#/overview?s=002085');
  const sheet = page.getByRole('dialog');
  await expect(sheet.getByRole('heading', { name: /相似情景/ })).toBeVisible({ timeout: 20_000 });
  await expect(sheet.locator('.analog-stats, .analogs-empty').first()).toBeVisible({ timeout: 30_000 });
});

test('agent surface: manifest and llms.txt describe the same capabilities', async ({ request }) => {
  const manifest = await (await request.get('/api/v1/agent/manifest')).json();
  const names: string[] = manifest.tools.map((tool: { name: string }) => tool.name);
  expect(names).toEqual(expect.arrayContaining(['screen_stocks', 'parse_question', 'pattern_scan', 'factor_lab']));
  const llms = await (await request.get('/llms.txt')).text();
  for (const name of names) expect(llms).toContain(name);
});
