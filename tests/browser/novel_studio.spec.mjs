import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

const runtimeIssues = new WeakMap();

test.beforeEach(async ({ page }, testInfo) => {
    const issues = [];
    runtimeIssues.set(page, issues);
    page.on('console', message => {
        // Firefox warns about this page's intentional sticky top bar; keep the
        // allowlist exact so all application warnings and errors still fail.
        const knownEngineWarning = testInfo.project.name === 'firefox'
            && message.type() === 'warning'
            && message.text().startsWith('[JavaScript Warning: "This site appears to use a scroll-linked positioning effect.');
        if (['warning', 'error'].includes(message.type()) && !knownEngineWarning) {
            issues.push(`console ${message.type()}: ${message.text()}`);
        }
    });
    page.on('pageerror', error => issues.push(`pageerror: ${error.message}`));
    page.on('request', request => {
        const url = new URL(request.url());
        if (['http:', 'https:'].includes(url.protocol) && url.hostname !== '127.0.0.1') {
            issues.push(`external request: ${url.origin}`);
        }
    });
});

test.afterEach(async ({ page }) => {
    expect(runtimeIssues.get(page), 'browser console and page errors').toEqual([]);
});

async function openDemoNovel(page) {
    const response = await page.goto('/');
    expect(response?.ok()).toBeTruthy();
    const shelfToggle = page.getByRole('button', { name: '书架', exact: true });
    if (await shelfToggle.isVisible()) await shelfToggle.click();
    const book = page.getByRole('button', { name: '打开作品：雾港来信', exact: true });
    await expect(book).toBeVisible();
    await book.click();
    await expect(page.getByRole('region', { name: '《雾港来信》', exact: true })).toBeVisible();
}

async function waitForNewCompletedTask(page, previousTaskId, novelId, expectedStep) {
    await expect.poll(async () => {
        const response = await page.request.get('/api/status');
        if (!response.ok()) return '';
        const status = await response.json();
        if (status.task_id === previousTaskId || status.is_running || status.error) return '';
        return `${status.current_novel}|${status.current_step}|${status.progress}`;
    }, { timeout: 30_000 }).toBe(`${novelId}|${expectedStep}|100`);
    await expect.poll(async () => {
        const response = await page.request.get(`/api/novel/${novelId}`);
        if (!response.ok()) return false;
        const project = await response.json();
        const browserRevision = await page.evaluate(() => novelData?.revision || '');
        return browserRevision === project.revision;
    }, { timeout: 10_000 }).toBe(true);
}

test('loads the offline library with security headers and named workspace landmarks', async ({ page }) => {
    const response = await page.goto('/');
    expect(response?.headers()['cache-control']).toContain('no-store');
    expect(response?.headers()['content-security-policy']).toContain("default-src 'self'");
    expect(response?.headers()['x-frame-options']).toBe('DENY');
    await expect(page).toHaveTitle('Moirai');
    await expect(page.locator('.topbar-brand-text')).toHaveText('Moirai');

    await expect(page.getByRole('complementary', { name: '作品库', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: /让 AI 像白金作家团队/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /^打开作品：/ })).toHaveCount(2);

    await openDemoNovel(page);
    await expect(page.getByRole('heading', { level: 1, name: '《雾港来信》', exact: true })).toBeVisible();
    await expect(page.getByRole('tablist', { name: '雾港来信 · 作品工作区', exact: true })).toBeVisible();
    await expect(page.getByRole('navigation', { name: '章节目录', exact: true })).toBeVisible();
    await expect(page.getByRole('article', { name: '第 1 章 旅程 1', exact: true })).toBeVisible();
});

test('opens all compliance findings and locates the selected paragraph', async ({ page }) => {
    const saved = await page.request.post('/api/compliance/custom_words', {
        data: { words: { 浏览器门禁: ['第 2 段', '第 10 段'] } },
    });
    expect(saved.ok()).toBeTruthy();
    await openDemoNovel(page);

    const summary = page.getByRole('button', { name: '查看 2 处合规风险', exact: true });
    await summary.click();
    await expect(summary).toHaveAttribute('aria-expanded', 'true');
    const panel = page.getByRole('complementary', { name: '读者与合规分析面板', exact: true });
    await expect(panel).toBeVisible();
    const findings = panel.getByRole('button', { name: /^定位合规风险：/ });
    await expect(findings).toHaveCount(2);
    await expect(findings.first()).toBeFocused({ timeout: 2_000 });

    await findings.nth(1).click();
    const paragraphs = page.locator('#current-chapter-body .reader-para');
    await expect.poll(() => paragraphs.nth(9).evaluate(element => element.style.background)).not.toBe('');
    expect(await paragraphs.first().evaluate(element => element.style.background)).toBe('');

    await page.keyboard.press('Escape');
    await expect(panel).toBeHidden();
    await expect(summary).toHaveAttribute('aria-expanded', 'false');
    await expect(summary).toBeFocused();
});

test('keeps the workspace inside four supported viewport widths', async ({ page }) => {
    for (const width of [320, 390, 768, 1024]) {
        await page.setViewportSize({ width, height: 844 });
        await openDemoNovel(page);
        const dimensions = await page.evaluate(() => ({
            viewport: window.innerWidth,
            document: document.documentElement.scrollWidth,
            body: document.body.scrollWidth,
            reader: document.getElementById('reader-scroll')?.getBoundingClientRect().width || 0,
        }));
        expect(dimensions.viewport).toBe(width);
        expect(dimensions.document).toBeLessThanOrEqual(width);
        expect(dimensions.body).toBeLessThanOrEqual(width);
        expect(dimensions.reader).toBeGreaterThan(0);
        expect(dimensions.reader).toBeLessThanOrEqual(width);
    }
});

test('keyboard tab navigation changes tools without changing chapters', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 844 });
    await openDemoNovel(page);
    const readerTab = page.getByRole('tab', { name: '沉浸阅读', exact: true });
    const graphTab = page.getByRole('tab', { name: '实体图谱', exact: true });
    await readerTab.focus();
    await readerTab.press('ArrowRight');
    await expect(graphTab).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('#current-chapter-title')).toHaveText('第 1 章 旅程 1');
});

test('searches lazy chapter prose and jumps without changing the project revision', async ({ page }) => {
    await openDemoNovel(page);
    const directory = page.getByRole('navigation', { name: '章节目录', exact: true });
    await directory.getByRole('button', { name: /第2章 旅程 2/ }).click();
    await expect(page.getByRole('article', { name: '第 2 章 旅程 2', exact: true })).toBeVisible();
    const before = await (await page.request.get('/api/novel/demo_alpha')).json();

    await page.keyboard.press('Control+K');
    const search = page.getByRole('combobox', { name: '搜索作品、章节或操作', exact: true });
    await expect(search).toBeFocused();
    await search.fill('远处的灯火');
    const chapterOne = page.getByRole('option', { name: /第1章 旅程 1/ });
    await expect(chapterOne).toContainText('远处的灯火');
    await chapterOne.click();

    await expect(page.getByRole('article', { name: '第 1 章 旅程 1', exact: true })).toBeVisible();
    const after = await (await page.request.get('/api/novel/demo_alpha')).json();
    expect(after.revision).toBe(before.revision);
});

test('exports a locked prose snapshot without changing the workspace revision', async ({ page }) => {
    await openDemoNovel(page);
    const before = await (await page.request.get('/api/novel/demo_alpha')).json();
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: 'TXT', exact: true }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe('demo_alpha_全本发布稿.txt');
    const after = await (await page.request.get('/api/novel/demo_alpha')).json();
    expect(after.revision).toBe(before.revision);
    await expect(page.getByRole('heading', { level: 1, name: '《雾港来信》', exact: true })).toBeVisible();
});

test('saves chapter text through the revision-bound editor and keeps the chapter selected', async ({ page }) => {
    await openDemoNovel(page);
    const chapterTwo = page.getByRole('navigation', { name: '章节目录', exact: true })
        .getByRole('button', { name: /第2章 旅程 2/ });
    await chapterTwo.click();
    await expect(page.getByRole('article', { name: '第 2 章 旅程 2', exact: true })).toBeVisible();

    await page.getByRole('button', { name: '编辑本章正文', exact: true }).click();
    const modal = page.locator('#modal-chapter');
    await expect(modal).toHaveClass(/open/);
    const editor = page.getByLabel('本章正文', { exact: true });
    await expect(editor).toBeVisible();
    const updated = '第 2 章已通过真实浏览器保存。\n\n版本保护和章节选择保持稳定。';
    await editor.fill(updated);
    await modal.getByRole('button', { name: '保存', exact: true }).click();

    await expect(modal).toBeHidden();
    await expect(page.getByRole('article', { name: '第 2 章 旅程 2', exact: true })).toContainText(updated);
    await expect(page.locator('#current-chapter-title')).toHaveText('第 2 章 旅程 2');
    await expect(chapterTwo).toHaveAttribute('aria-current', 'page');
});

test('round-trips a real backup through deletion, import, rollback and trash restore', async ({ page }) => {
    await openDemoNovel(page);
    const originalBody = await page.locator('#current-chapter-body').innerText();

    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: '备份项目', exact: true }).click();
    const backup = await downloadPromise;
    expect(backup.suggestedFilename()).toBe('demo_alpha_backup.zip');
    const backupPath = await backup.path();
    expect(backupPath).toBeTruthy();

    const deleteAlpha = page.getByRole('button', { name: '删除作品：雾港来信', exact: true });
    await deleteAlpha.click({ force: true });
    const confirm = page.locator('#modal-confirm');
    await expect(confirm).toHaveClass(/open/);
    await expect(confirm.getByRole('heading', { name: '移除作品', exact: true })).toBeVisible();
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect(confirm).toBeHidden();
    await expect(page.getByRole('button', { name: '打开作品：雾港来信', exact: true })).toHaveCount(0);
    await expect(page.getByText('作品已移入本机回收目录', { exact: true })).toBeVisible();

    await page.locator('#import-file').setInputFiles({
        name: 'demo_alpha_backup.zip',
        mimeType: 'application/zip',
        buffer: readFileSync(backupPath),
    });
    await expect(page.getByText(/已导入《雾港来信》/)).toBeVisible();
    const importedAlpha = page.getByRole('button', { name: '打开作品：雾港来信', exact: true });
    await expect(importedAlpha).toHaveCount(1);
    await importedAlpha.click();
    await expect(page.getByRole('article', { name: '第 1 章 旅程 1', exact: true })).toBeVisible();
    await expect.poll(() => page.locator('#current-chapter-body').innerText()).toBe(originalBody);

    await page.getByRole('button', { name: '时光机回滚', exact: true }).click();
    const rollback = page.locator('#modal-rollback');
    await expect(rollback).toHaveClass(/open/);
    const target = page.locator('#rollback-target');
    await expect(target).toBeEnabled();
    await target.selectOption('1');
    await rollback.getByRole('button', { name: '确认回滚', exact: true }).click();
    await expect(rollback).toBeHidden();
    await expect(page.getByText('已回滚至第 1 章（图谱与伏笔同步还原）', { exact: true })).toBeVisible();
    await expect(page.getByRole('article', { name: '第 1 章 旅程 1', exact: true })).toBeVisible();
    await expect(page.getByRole('navigation', { name: '章节目录', exact: true })
        .getByRole('button', { name: /第2章 旅程 2/ })).toHaveCount(0);

    // All Playwright projects share this disposable webServer. Restore the
    // original two-chapter fixture before the next engine starts.
    const deleteRolledBackAlpha = page.getByRole('button', { name: '删除作品：雾港来信', exact: true });
    await deleteRolledBackAlpha.click({ force: true });
    await expect(confirm).toHaveClass(/open/);
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect(page.getByRole('button', { name: '打开作品：雾港来信', exact: true })).toHaveCount(0);
    await page.locator('#import-file').setInputFiles({
        name: 'demo_alpha_backup.zip',
        mimeType: 'application/zip',
        buffer: readFileSync(backupPath),
    });
    const resetAlpha = page.getByRole('button', { name: '打开作品：雾港来信', exact: true });
    await expect(resetAlpha).toHaveCount(1);
    await resetAlpha.click();
    await expect(page.getByRole('navigation', { name: '章节目录', exact: true })
        .getByRole('button', { name: /第2章 旅程 2/ })).toHaveCount(1);

    const deleteBeta = page.getByRole('button', { name: '删除作品：群星航路', exact: true });
    await deleteBeta.click({ force: true });
    await expect(confirm).toHaveClass(/open/);
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect(page.getByRole('button', { name: '打开作品：群星航路', exact: true })).toHaveCount(0);

    await page.getByRole('button', { name: '打开作品回收目录', exact: true }).click();
    const trash = page.locator('#modal-trash');
    await expect(trash).toHaveClass(/open/);
    const betaEntry = page.locator('#trash-list .card-flat').filter({ hasText: '群星航路' });
    await expect(betaEntry).toBeVisible();
    await betaEntry.getByRole('button', { name: '恢复', exact: true }).click();
    await expect(confirm).toHaveClass(/open/);
    await confirm.getByRole('button', { name: '恢复', exact: true }).click();
    await expect(page.getByText('已恢复作品「demo_beta」', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '打开作品：群星航路', exact: true })).toHaveCount(1);
});

test('creates, continues, rewrites and delivers a planned novel through real background jobs', async ({ page }, testInfo) => {
    test.setTimeout(45_000);
    const response = await page.goto('/');
    expect(response?.ok()).toBeTruthy();
    const projectId = `browser_created_${testInfo.project.name}`;

    await page.getByRole('button', { name: '新建作品', exact: true }).first().click();
    const create = page.locator('#modal-create');
    await expect(create).toHaveClass(/open/);
    await page.getByLabel('项目标识名', { exact: true }).fill(projectId);
    await page.getByLabel('核心创意与金手指运作机制', { exact: true })
        .fill('守夜人在断电星港收到一封来自失踪航路的纸质来信。');
    await page.getByLabel('首批全自动生成章节数', { exact: true }).fill('1');
    await page.getByLabel('全书规划总卷数', { exact: true }).fill('1');
    await create.getByRole('button', { name: '启动全自动生产', exact: true }).click();

    await expect(create).toBeHidden();
    await expect(page.getByText('生产流水线已启动，可在顶部查看实时进度', { exact: true })).toBeVisible();
    await expect.poll(async () => {
        const statusResponse = await page.request.get('/api/status');
        const status = await statusResponse.json();
        return {
            novel: status.current_novel,
            running: status.is_running,
            step: status.current_step,
            progress: status.progress,
            error: status.error,
        };
    }, { timeout: 30_000 }).toEqual({
        novel: projectId,
        running: false,
        step: '全部生成完成',
        progress: 100,
        error: '',
    });

    await expect(page.getByText('全部生成完成', { exact: true })).toBeVisible({ timeout: 10_000 });
    const generatedBook = page.getByRole('button', { name: '打开作品：离线星港档案', exact: true });
    await expect(generatedBook).toHaveCount(1);
    await generatedBook.click();
    await expect(page.getByRole('heading', { level: 1, name: '《离线星港档案》', exact: true })).toBeVisible();
    await expect(page.getByRole('article', { name: '第 1 章 停电后的第 1 封信', exact: true })).toBeVisible();
    await expect(page.locator('#current-chapter-body')).toContainText('罗盘却指向港墙之外');

    const projectResponse = await page.request.get(`/api/novel/${projectId}`);
    expect(projectResponse.ok()).toBeTruthy();
    const project = await projectResponse.json();
    expect(project.summary.current_chapter).toBe(1);
    expect(project.summary.planning_pending).toBeFalsy();
    expect(project.chapters).toHaveLength(1);
    expect(project.chapters[0].audit_history.at(-1).passed).toBeTruthy();

    let previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '批量续写', exact: true }).click();
    const continuation = page.locator('#modal-continue');
    await expect(continuation).toHaveClass(/open/);
    await page.getByLabel('本次续写章节数', { exact: true }).fill('2');
    await continuation.getByRole('button', { name: '开始续写', exact: true }).click();
    await expect(continuation).toBeHidden();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '全部生成完成');

    const directory = page.getByRole('navigation', { name: '章节目录', exact: true });
    await expect(directory.getByRole('button', { name: /^第[123]章 / })).toHaveCount(3, { timeout: 10_000 });
    await page.getByRole('tab', { name: '商业宣发', exact: true }).click();
    await expect(page.locator('#delivery-script-box')).toContainText('全港断电后');
    await expect(page.locator('#delivery-prompts-box')).toContainText('主封面');

    await page.getByRole('tab', { name: '沉浸阅读', exact: true }).click();
    await directory.getByRole('button', { name: /第2章 停电后的第 2 封信/ }).click();
    await expect(page.getByRole('article', { name: '第 2 章 停电后的第 2 封信', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '推演读者弹幕与留存', exact: true }).click();
    await expect(page.locator('#stat-sentiment-summary')).toContainText('离线读者认为');
    await expect(page.locator('#retention-pct')).toHaveText('92.5%');
    await page.locator('#rail-backdrop').click({ force: true });
    await expect(page.locator('#reader-rail')).not.toHaveClass(/open/);
    await page.getByRole('tab', { name: '分支推演', exact: true }).click();
    await page.getByRole('button', { name: '推演当前章节分支', exact: true }).click();
    await expect(page.locator('#branching-results-container')).toContainText('封锁旧观测站');
    await page.getByRole('tab', { name: '沉浸阅读', exact: true }).click();
    await page.getByRole('button', { name: '编辑细纲', exact: true }).click();
    const outline = page.locator('#modal-outline');
    await expect(outline).toHaveClass(/open/);
    await page.getByLabel('章节标题', { exact: true }).fill('重写验证：第二封信');
    await outline.getByRole('button', { name: '保存', exact: true }).click();
    await expect(outline).toBeHidden();
    let currentProject = await (await page.request.get(`/api/novel/${projectId}`)).json();
    expect(currentProject.branch_simulations['2']).toBeUndefined();
    expect(currentProject.reader_simulations['2']).toBeTruthy();
    expect(currentProject.short_dramas['2']).toBeTruthy();

    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '整章重新生成', exact: true }).click();
    const confirmRewrite = page.locator('#modal-confirm');
    await expect(confirmRewrite).toHaveClass(/open/);
    await expect(confirmRewrite.getByRole('heading', { name: '重写本章', exact: true })).toBeVisible();
    await confirmRewrite.getByRole('button', { name: '重写', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '重写完成');
    await expect(page.getByRole('article', { name: '第 2 章 重写验证：第二封信', exact: true })).toBeVisible({ timeout: 10_000 });

    currentProject = await (await page.request.get(`/api/novel/${projectId}`)).json();
    expect(currentProject.delivery_package).toBeNull();
    expect(currentProject.short_dramas['2']).toBeUndefined();
    expect(currentProject.reader_simulations['2']).toBeUndefined();
    expect(currentProject.branch_simulations['2']).toBeUndefined();

    await page.getByRole('tab', { name: '短剧与演播', exact: true }).click();
    await expect(page.locator('#short-drama-box')).toContainText('本章暂无分镜剧本');
    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '重新生成当前章节短剧分镜', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '短剧分镜生成完成');
    await expect(page.locator('#short-drama-box')).toContainText('重写验证：第二封信', { timeout: 10_000 });

    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '重新生成有声演播配置', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '有声演播配置生成完成');
    await expect(page.locator('#audio-cast-box')).toContainText('林岚', { timeout: 10_000 });

    await page.getByRole('tab', { name: '商业宣发', exact: true }).click();
    await expect(page.locator('#delivery-script-box')).toContainText('尚未生成宣发包');

    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '重新生成', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '商业交付包生成完成');
    await expect(page.locator('#delivery-script-box')).toContainText('重写验证：第二封信', { timeout: 10_000 });
    currentProject = await (await page.request.get(`/api/novel/${projectId}`)).json();
    expect(currentProject.summary.current_chapter).toBe(3);
    expect(currentProject.delivery_package.cost_and_token_estimate.total_words_generated).toBeGreaterThan(0);

    // Leave the shared server with the same two active fixtures for the next
    // engine; the generated project remains recoverable in the demo trash.
    await page.getByRole('button', { name: '删除作品：离线星港档案', exact: true }).click({ force: true });
    const confirm = page.locator('#modal-confirm');
    await expect(confirm).toHaveClass(/open/);
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect(generatedBook).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^打开作品：/ })).toHaveCount(2);
});
