import { defineConfig } from '@playwright/test';

const baseURL = process.env.SAFARI_BASE_URL || 'http://127.0.0.1:8769';
const target = new URL(baseURL);
if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || !target.port) {
    throw new Error('SAFARI_BASE_URL must be an explicit 127.0.0.1 HTTP endpoint');
}
const port = Number.parseInt(target.port, 10);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('SAFARI_BASE_URL contains an invalid port');
}

process.env.SAFARI_BASE_URL = baseURL;
export default defineConfig({
    testDir: './tests/browser',
    testMatch: 'novel_studio.spec.mjs',
    fullyParallel: false,
    workers: 1,
    forbidOnly: Boolean(process.env.CI),
    retries: process.env.CI ? 1 : 0,
    reporter: [['line']],
    outputDir: 'test-results',
    use: {
        baseURL,
        trace: 'retain-on-failure',
    },
    webServer: {
        command: `uv run --isolated --no-project --python 3.11 --with-requirements requirements-dev.lock python tests/browser/serve_safari_demo.py --port ${port}`,
        url: `${baseURL}/api/novels`,
        reuseExistingServer: false,
        timeout: 180_000,
        env: {
            ...process.env,
            OPENAI_API_KEY: '',
            OPENAI_BASE_URL: 'http://127.0.0.1:9/v1',
        },
    },
    projects: [{name:'safari'}],
});
