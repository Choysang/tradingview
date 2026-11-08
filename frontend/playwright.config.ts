import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Set PULSE_CHROMIUM when the bundled Playwright browser is not installed,
// for example PULSE_CHROMIUM=C:/path/to/chrome.exe.
const executablePath = process.env.PULSE_CHROMIUM;

export default defineConfig({
  testDir: './e2e',
  // A fresh directory per run: Playwright wipes its output folder at start, and
  // an ever-growing trace folder inside the repo trips bulk-delete guards.
  outputDir: join(tmpdir(), 'pulse-e2e', String(Date.now())),
  workers: 1,
  fullyParallel: false,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.PULSE_BASE_URL ?? 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: executablePath ? { executablePath } : {},
  },
});
