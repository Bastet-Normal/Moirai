import { expect, test as runner } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { launchSafari, watchDownload as watchSafariDownload } from './native-safari.mjs';

async function watchWebKitDownload(page) {
    const pending = page.waitForEvent('download');
    return async () => {
        const download = await pending;
        return {name: download.suggestedFilename(), read: async () => readFile(await download.path())};
    };
}

export const test = runner.extend({
    studioPage: async ({ playwright, baseURL }, use, testInfo) => {
        const native = testInfo.project.name === 'safari';
        const browser = native ? await launchSafari() : await playwright.webkit.launch({headless:true});
        try {
            const page = await browser.newPage({baseURL, viewport:{width:1280, height:800}});
            await use(page);
            expect(await page.evaluate(() => window.safariIssues)).toEqual([]);
        } finally {
            await browser.close();
        }
    },
    watchDownload: async ({}, use, testInfo) => {
        await use(testInfo.project.name === 'safari' ? watchSafariDownload : watchWebKitDownload);
    },
});
