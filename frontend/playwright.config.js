import { defineConfig } from '@playwright/test';
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1, timeout: 20000,
  use: { baseURL: 'http://127.0.0.1:18106', headless: true, launchOptions: { ...(executablePath ? { executablePath } : {}) }, trace: 'retain-on-failure' },
  webServer: { command: 'npm run dev -- --port 18106', url: 'http://127.0.0.1:18106', reuseExistingServer: false, timeout: 30000 },
});
