import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:8769';
const target = new URL(baseURL);
if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || !target.port) {
    throw new Error('PLAYWRIGHT_BASE_URL must be an explicit 127.0.0.1 HTTP endpoint');
}
const port = Number.parseInt(target.port, 10);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('PLAYWRIGHT_BASE_URL contains an invalid port');
}

export default defineConfig({
    testDir: './tests/browser',
    fullyParallel: false,
    workers: 1,
    forbidOnly: Boolean(process.env.CI),
    retries: process.env.CI ? 1 : 0,
    reporter: [['line']],
    outputDir: 'test-results',
    use: {
        baseURL,
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
    },
    webServer: {
        command: `uv run --isolated --no-project --python 3.11 --with-requirements requirements-dev.lock python scripts/serve_demo.py --port ${port}`,
        url: `${baseURL}/api/novels`,
        reuseExistingServer: false,
        timeout: 180_000,
        env: {
            ...process.env,
            OPENAI_API_KEY: '',
            OPENAI_BASE_URL: 'http://127.0.0.1:9/v1',
        },
    },
    projects: [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
        { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
        { name: 'webkit', use: { ...devices['Desktop Safari'] } },
    ],
});
