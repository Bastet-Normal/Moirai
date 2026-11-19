import { expect, test as runner } from '@playwright/test';
import { launchSafari, watchDownload } from './native-safari.mjs';

const test = runner.extend({
    safariPage: async ({}, use, testInfo) => {
        const browser = await launchSafari();
        try {
            const page = await browser.newPage({viewport:{width:1280,height:800}});
            await use(page);
            expect(await page.evaluate(() => window.safariIssues)).toEqual([]);
        } finally { await browser.close(); }
    }
});

async function openDemoNovel(page) {
    const response = await page.goto('/');
    expect(response?.ok).toBeTruthy();
    const shelfToggle = page.getByRole('button', { name: '书架', exact: true });
    if (await shelfToggle.isVisible()) await shelfToggle.click();
    const book = page.getByRole('button', { name: '打开作品：雾港来信', exact: true });
    await expect.poll(() => book.isVisible()).toBe(true);
    await book.click();
    await expect.poll(() => page.getByRole('region', { name: '《雾港来信》', exact: true }).isVisible()).toBe(true);
}

async function waitForNewCompletedTask(page, previousTaskId, novelId, expectedStep) {
    await expect.poll(async () => {
        const response = await page.request.get('/api/status');
        if (!response.ok) return '';
        const status = await response.json();
        if (status.task_id === previousTaskId || status.is_running || status.error) return '';
        return `${status.current_novel}|${status.current_step}|${status.progress}`;
    }, { timeout: 30_000 }).toBe(`${novelId}|${expectedStep}|100`);
    await expect.poll(async () => {
        const response = await page.request.get(`/api/novel/${novelId}`);
        if (!response.ok) return false;
        const project = await response.json();
        const browserRevision = await page.evaluate(() => novelData?.revision || '');
        return browserRevision === project.revision;
    }, { timeout: 10_000 }).toBe(true);
}

test('loads the offline library with security headers and named workspace landmarks', async ({ safariPage: page }) => {
    const response = await page.goto('/');
    expect(response?.headers.get('cache-control')).toContain('no-store');
    expect(response?.headers.get('content-security-policy')).toContain("default-src 'self'");
    expect(response?.headers.get('x-frame-options')).toBe('DENY');
    await expect.poll(() => page.title()).toBe('Moirai');
    await expect.poll(() => page.locator('.topbar-brand-text').innerText()).toBe('Moirai');

    await expect.poll(() => page.getByRole('complementary', { name: '作品库', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('heading', { name: /让 AI 像白金作家团队/ }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('button', { name: /^打开作品：/ }).count()).toBe(2);

    await openDemoNovel(page);
    await expect.poll(() => page.getByRole('heading', { level: 1, name: '《雾港来信》', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('tablist', { name: '雾港来信 · 作品工作区', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('navigation', { name: '章节目录', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('article', { name: '第 1 章 旅程 1', exact: true }).isVisible()).toBe(true);
});

test('opens all compliance findings and locates the selected paragraph', async ({ safariPage: page }) => {
    const saved = await page.request.post('/api/compliance/custom_words', {
        data: { words: { 浏览器门禁: ['第 2 段', '第 10 段'] } },
    });
    expect(saved.ok).toBeTruthy();
    await openDemoNovel(page);

    const summary = page.getByRole('button', { name: '查看 2 处合规风险', exact: true });
    await summary.click();
    await expect.poll(() => summary.getAttribute('aria-expanded')).toBe('true');
    const panel = page.getByRole('complementary', { name: '读者与合规分析面板', exact: true });
    await expect.poll(() => panel.isVisible()).toBe(true);
    const findings = panel.getByRole('button', { name: /^定位合规风险：/ });
    await expect.poll(() => findings.count()).toBe(2);
    await expect.poll(() => page.evaluate(() => document.activeElement?.getAttribute('aria-label')), { timeout: 5_000 })
        .toBe(await findings.first().getAttribute('aria-label'));

    await findings.nth(1).click();
    const paragraphs = page.locator('#current-chapter-body .reader-para');
    await expect.poll(() => paragraphs.nth(9).evaluate(element => element.style.background)).not.toBe('');
    expect(await paragraphs.first().evaluate(element => element.style.background)).toBe('');

    await page.keyboard.press('Escape');
    await expect.poll(() => panel.isVisible()).toBe(false);
    await expect.poll(() => summary.getAttribute('aria-expanded')).toBe('false');
    await expect.poll(() => summary.focused()).toBe(true);
});

test('keeps the workspace inside four supported viewport widths', async ({ safariPage: page }) => {
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

test('keyboard tab navigation changes tools without changing chapters', async ({ safariPage: page }) => {
    await page.setViewportSize({ width: 768, height: 844 });
    await openDemoNovel(page);
    const readerTab = page.getByRole('tab', { name: '沉浸阅读', exact: true });
    const graphTab = page.getByRole('tab', { name: '实体图谱', exact: true });
    await readerTab.focus();
    await readerTab.press('ArrowRight');
    await expect.poll(() => graphTab.getAttribute('aria-selected')).toBe('true');
    await expect.poll(() => page.locator('#current-chapter-title').innerText()).toBe('第 1 章 旅程 1');
});

test('searches lazy chapter prose and jumps without changing the project revision', async ({ safariPage: page }) => {
    await openDemoNovel(page);
    const directory = page.getByRole('navigation', { name: '章节目录', exact: true });
    await directory.getByRole('button', { name: /第2章 旅程 2/ }).click();
    await expect.poll(() => page.getByRole('article', { name: '第 2 章 旅程 2', exact: true }).isVisible()).toBe(true);
    const before = await (await page.request.get('/api/novel/demo_alpha')).json();

    await directory.getByRole('button', { name: /第2章 旅程 2/ }).focus();
    await page.keyboard.press('Meta+K');
    const search = page.getByRole('combobox', { name: '搜索作品、章节或操作', exact: true });
    await expect.poll(() => search.focused()).toBe(true);
    await search.fill('远处的灯火');
    const chapterOne = page.getByRole('option', { name: /第1章 旅程 1/ });
    await expect.poll(() => chapterOne.isVisible(), {timeout:10000}).toBe(true);
    await expect.poll(() => chapterOne.innerText()).toContain('远处的灯火');
    await chapterOne.click();

    await expect.poll(() => page.getByRole('article', { name: '第 1 章 旅程 1', exact: true }).isVisible()).toBe(true);
    const after = await (await page.request.get('/api/novel/demo_alpha')).json();
    expect(after.revision).toBe(before.revision);
});

test('exports a locked prose snapshot without changing the workspace revision', async ({ safariPage: page }) => {
    await openDemoNovel(page);
    const before = await (await page.request.get('/api/novel/demo_alpha')).json();
    const collectDownload = await watchDownload(page, '.txt');
    await page.getByRole('button', { name: 'TXT', exact: true }).click();
    const download = await collectDownload();
    expect(download.name).toBe('demo_alpha_全本发布稿.txt');
    expect((await download.read()).length).toBeGreaterThan(0);
    const after = await (await page.request.get('/api/novel/demo_alpha')).json();
    expect(after.revision).toBe(before.revision);
    await expect.poll(() => page.getByRole('heading', { level: 1, name: '《雾港来信》', exact: true }).isVisible()).toBe(true);
});

test('saves chapter text through the revision-bound editor and keeps the chapter selected', async ({ safariPage: page }) => {
    await openDemoNovel(page);
    const chapterTwo = page.getByRole('navigation', { name: '章节目录', exact: true })
        .getByRole('button', { name: /第2章 旅程 2/ });
    await chapterTwo.click();
    await expect.poll(() => page.getByRole('article', { name: '第 2 章 旅程 2', exact: true }).isVisible()).toBe(true);

    await page.getByRole('button', { name: '编辑本章正文', exact: true }).click();
    const modal = page.locator('#modal-chapter');
    await expect.poll(() => modal.getAttribute('class')).toMatch(/open/);
    const editor = page.getByLabel('本章正文', { exact: true });
    await expect.poll(() => editor.isVisible()).toBe(true);
    const updated = '第 2 章已通过真实浏览器保存。\n\n版本保护和章节选择保持稳定。';
    await editor.fill(updated);
    await modal.getByRole('button', { name: '保存', exact: true }).click();

    await expect.poll(() => modal.isVisible()).toBe(false);
    await expect.poll(() => page.getByRole('article', { name: '第 2 章 旅程 2', exact: true }).innerText().then(text => text.replace(/\s+/g,' '))).toContain(updated.replace(/\s+/g,' '));
    await expect.poll(() => page.locator('#current-chapter-title').innerText()).toBe('第 2 章 旅程 2');
    await expect.poll(() => chapterTwo.getAttribute('aria-current')).toBe('page');
});

test('round-trips a real backup through deletion, import, rollback and trash restore', async ({ safariPage: page }) => {
    await openDemoNovel(page);
    const originalBody = await page.locator('#current-chapter-body').innerText();

    const collectDownload = await watchDownload(page, '.zip');
    await page.getByRole('button', { name: '备份项目', exact: true }).click();
    const backup = await collectDownload();
    expect(backup.name).toBe('demo_alpha_backup.zip');
    const archive = await backup.read();
    expect(archive.length).toBeGreaterThan(0);

    const deleteAlpha = page.getByRole('button', { name: '删除作品：雾港来信', exact: true });
    await deleteAlpha.click({ force: true });
    const confirm = page.locator('#modal-confirm');
    await expect.poll(() => confirm.getAttribute('class')).toMatch(/open/);
    await expect.poll(() => confirm.getByRole('heading', { name: '移除作品', exact: true }).isVisible()).toBe(true);
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect.poll(() => confirm.isVisible()).toBe(false);
    await expect.poll(() => page.getByRole('button', { name: '打开作品：雾港来信', exact: true }).count()).toBe(0);
    await expect.poll(() => page.getByText('作品已移入本机回收目录', { exact: true }).isVisible()).toBe(true);

    await page.locator('#import-file').setInputFiles({
        name: 'demo_alpha_backup.zip',
        mimeType: 'application/zip',
        buffer: archive,
    });
    await expect.poll(() => page.getByText(/已导入《雾港来信》/).isVisible()).toBe(true);
    const importedAlpha = page.getByRole('button', { name: '打开作品：雾港来信', exact: true });
    await expect.poll(() => importedAlpha.count()).toBe(1);
    await importedAlpha.click();
    await expect.poll(() => page.getByRole('article', { name: '第 1 章 旅程 1', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.locator('#current-chapter-body').innerText()).toBe(originalBody);

    await page.getByRole('button', { name: '时光机回滚', exact: true }).click();
    const rollback = page.locator('#modal-rollback');
    await expect.poll(() => rollback.getAttribute('class')).toMatch(/open/);
    const target = page.locator('#rollback-target');
    await expect.poll(() => target.enabled()).toBe(true);
    await target.selectOption('1');
    await rollback.getByRole('button', { name: '确认回滚', exact: true }).click();
    await expect.poll(() => rollback.isVisible()).toBe(false);
    await expect.poll(() => page.getByText('已回滚至第 1 章（图谱与伏笔同步还原）', { exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('article', { name: '第 1 章 旅程 1', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('navigation', { name: '章节目录', exact: true })
        .getByRole('button', { name: /第2章 旅程 2/ }).count()).toBe(0);

    // Restore the original two-chapter fixture for subsequent scenarios.
    const deleteRolledBackAlpha = page.getByRole('button', { name: '删除作品：雾港来信', exact: true });
    await deleteRolledBackAlpha.click({ force: true });
    await expect.poll(() => confirm.getAttribute('class')).toMatch(/open/);
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect.poll(() => page.getByRole('button', { name: '打开作品：雾港来信', exact: true }).count()).toBe(0);
    await page.locator('#import-file').setInputFiles({
        name: 'demo_alpha_backup.zip',
        mimeType: 'application/zip',
        buffer: archive,
    });
    const resetAlpha = page.getByRole('button', { name: '打开作品：雾港来信', exact: true });
    await expect.poll(() => resetAlpha.count()).toBe(1);
    await resetAlpha.click();
    await expect.poll(() => page.getByRole('navigation', { name: '章节目录', exact: true })
        .getByRole('button', { name: /第2章 旅程 2/ }).count()).toBe(1);

    const deleteBeta = page.getByRole('button', { name: '删除作品：群星航路', exact: true });
    await deleteBeta.click({ force: true });
    await expect.poll(() => confirm.getAttribute('class')).toMatch(/open/);
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect.poll(() => page.getByRole('button', { name: '打开作品：群星航路', exact: true }).count()).toBe(0);

    await page.getByRole('button', { name: '打开作品回收目录', exact: true }).click();
    const trash = page.locator('#modal-trash');
    await expect.poll(() => trash.getAttribute('class')).toMatch(/open/);
    const betaEntry = page.locator('#trash-list .card-flat').filter({ hasText: '群星航路' });
    await expect.poll(() => betaEntry.isVisible()).toBe(true);
    await betaEntry.getByRole('button', { name: '恢复', exact: true }).click();
    await expect.poll(() => confirm.getAttribute('class')).toMatch(/open/);
    await confirm.getByRole('button', { name: '恢复', exact: true }).click();
    await expect.poll(() => page.getByText('已恢复作品「demo_beta」', { exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('button', { name: '打开作品：群星航路', exact: true }).count()).toBe(1);
});

test('creates, continues, rewrites and delivers a planned novel through real background jobs', async ({ safariPage: page }, testInfo) => {
    test.setTimeout(45_000);
    const response = await page.goto('/');
    expect(response?.ok).toBeTruthy();
    const projectId = `browser_created_${testInfo.project.name}_${testInfo.retry}`;

    await page.getByRole('button', { name: '新建作品', exact: true }).first().click();
    const create = page.locator('#modal-create');
    await expect.poll(() => create.getAttribute('class')).toMatch(/open/);
    await page.getByLabel('项目标识名', { exact: true }).fill(projectId);
    await page.getByLabel('核心创意与金手指运作机制', { exact: true })
        .fill('守夜人在断电星港收到一封来自失踪航路的纸质来信。');
    await page.getByLabel('首批全自动生成章节数', { exact: true }).fill('1');
    await page.getByLabel('全书规划总卷数', { exact: true }).fill('1');
    await create.getByRole('button', { name: '启动全自动生产', exact: true }).click();

    await expect.poll(() => create.isVisible()).toBe(false);
    await expect.poll(() => page.getByText('生产流水线已启动，可在顶部查看实时进度', { exact: true }).isVisible()).toBe(true);
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

    await expect.poll(() => page.getByText('全部生成完成', { exact: true }).isVisible(), { timeout: 10_000 }).toBe(true);
    const generatedBook = page.getByRole('button', { name: '打开作品：离线星港档案', exact: true });
    await expect.poll(() => generatedBook.count()).toBe(1);
    await generatedBook.click();
    await expect.poll(() => page.getByRole('heading', { level: 1, name: '《离线星港档案》', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.getByRole('article', { name: '第 1 章 停电后的第 1 封信', exact: true }).isVisible()).toBe(true);
    await expect.poll(() => page.locator('#current-chapter-body').innerText()).toContain('罗盘却指向港墙之外');

    const projectResponse = await page.request.get(`/api/novel/${projectId}`);
    expect(projectResponse.ok).toBeTruthy();
    const project = await projectResponse.json();
    expect(project.summary.current_chapter).toBe(1);
    expect(project.summary.planning_pending).toBeFalsy();
    expect(project.chapters).toHaveLength(1);
    expect(project.chapters[0].audit_history.at(-1).passed).toBeTruthy();

    let previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '批量续写', exact: true }).click();
    const continuation = page.locator('#modal-continue');
    await expect.poll(() => continuation.getAttribute('class')).toMatch(/open/);
    await page.getByLabel('本次续写章节数', { exact: true }).fill('2');
    await continuation.getByRole('button', { name: '开始续写', exact: true }).click();
    await expect.poll(() => continuation.isVisible()).toBe(false);
    await waitForNewCompletedTask(page, previousTaskId, projectId, '全部生成完成');

    const directory = page.getByRole('navigation', { name: '章节目录', exact: true });
    await expect.poll(() => directory.getByRole('button', { name: /^第[123]章 / }).count(), { timeout: 10_000 }).toBe(3);
    await page.getByRole('tab', { name: '商业宣发', exact: true }).click();
    await expect.poll(() => page.locator('#delivery-script-box').innerText()).toContain('全港断电后');
    await expect.poll(() => page.locator('#delivery-prompts-box').innerText()).toContain('主封面');

    await page.getByRole('tab', { name: '沉浸阅读', exact: true }).click();
    await directory.getByRole('button', { name: /第2章 停电后的第 2 封信/ }).click();
    await expect.poll(() => page.getByRole('article', { name: '第 2 章 停电后的第 2 封信', exact: true }).isVisible()).toBe(true);
    await page.getByRole('button', { name: '推演读者弹幕与留存', exact: true }).click();
    await expect.poll(() => page.locator('#stat-sentiment-summary').innerText()).toContain('离线读者认为');
    await expect.poll(() => page.locator('#retention-pct').innerText()).toBe('92.5%');
    await page.locator('#rail-backdrop').click({ force: true });
    await expect.poll(() => page.locator('#reader-rail').getAttribute('class')).not.toMatch(/open/);
    await page.getByRole('tab', { name: '分支推演', exact: true }).click();
    await page.getByRole('button', { name: '推演当前章节分支', exact: true }).click();
    await expect.poll(() => page.locator('#branching-results-container').innerText()).toContain('封锁旧观测站');
    await page.getByRole('tab', { name: '沉浸阅读', exact: true }).click();
    await page.getByRole('button', { name: '编辑细纲', exact: true }).click();
    const outline = page.locator('#modal-outline');
    await expect.poll(() => outline.getAttribute('class')).toMatch(/open/);
    await page.getByLabel('章节标题', { exact: true }).fill('重写验证：第二封信');
    await outline.getByRole('button', { name: '保存', exact: true }).click();
    await expect.poll(() => outline.isVisible()).toBe(false);
    let currentProject = await (await page.request.get(`/api/novel/${projectId}`)).json();
    expect(currentProject.branch_simulations['2']).toBeUndefined();
    expect(currentProject.reader_simulations['2']).toBeTruthy();
    expect(currentProject.short_dramas['2']).toBeTruthy();

    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '整章重新生成', exact: true }).click();
    const confirmRewrite = page.locator('#modal-confirm');
    await expect.poll(() => confirmRewrite.getAttribute('class')).toMatch(/open/);
    await expect.poll(() => confirmRewrite.getByRole('heading', { name: '重写本章', exact: true }).isVisible()).toBe(true);
    await confirmRewrite.getByRole('button', { name: '重写', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '重写完成');
    await expect.poll(() => page.getByRole('article', { name: '第 2 章 重写验证：第二封信', exact: true }).isVisible(), { timeout: 10_000 }).toBe(true);

    currentProject = await (await page.request.get(`/api/novel/${projectId}`)).json();
    expect(currentProject.delivery_package).toBeNull();
    expect(currentProject.short_dramas['2']).toBeUndefined();
    expect(currentProject.reader_simulations['2']).toBeUndefined();
    expect(currentProject.branch_simulations['2']).toBeUndefined();

    await page.getByRole('tab', { name: '短剧与演播', exact: true }).click();
    await expect.poll(() => page.locator('#short-drama-box').innerText()).toContain('本章暂无分镜剧本');
    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '重新生成当前章节短剧分镜', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '短剧分镜生成完成');
    await expect.poll(() => page.locator('#short-drama-box').innerText(), { timeout: 10_000 }).toContain('重写验证：第二封信');

    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '重新生成有声演播配置', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '有声演播配置生成完成');
    await expect.poll(() => page.locator('#audio-cast-box').innerText(), { timeout: 10_000 }).toContain('林岚');

    await page.getByRole('tab', { name: '商业宣发', exact: true }).click();
    await expect.poll(() => page.locator('#delivery-script-box').innerText()).toContain('尚未生成宣发包');

    previousTaskId = (await (await page.request.get('/api/status')).json()).task_id;
    await page.getByRole('button', { name: '重新生成', exact: true }).click();
    await waitForNewCompletedTask(page, previousTaskId, projectId, '商业交付包生成完成');
    await expect.poll(() => page.locator('#delivery-script-box').innerText(), { timeout: 10_000 }).toContain('重写验证：第二封信');
    currentProject = await (await page.request.get(`/api/novel/${projectId}`)).json();
    expect(currentProject.summary.current_chapter).toBe(3);
    expect(currentProject.delivery_package.cost_and_token_estimate.total_words_generated).toBeGreaterThan(0);

    // Leave the shared server with the same two active fixtures for the next
    // engine; the generated project remains recoverable in the demo trash.
    await page.getByRole('button', { name: '删除作品：离线星港档案', exact: true }).click({ force: true });
    const confirm = page.locator('#modal-confirm');
    await expect.poll(() => confirm.getAttribute('class')).toMatch(/open/);
    await confirm.getByRole('button', { name: '移入回收目录', exact: true }).click();
    await expect.poll(() => generatedBook.count()).toBe(0);
    await expect.poll(() => page.getByRole('button', { name: /^打开作品：/ }).count()).toBe(2);
});
