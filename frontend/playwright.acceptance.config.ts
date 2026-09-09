import { defineConfig, devices } from "@playwright/test"

export default defineConfig({
  testDir: "./tests",
  testMatch: /acceptance-batch\.spec\.ts/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 600_000,
  expect: { timeout: 20_000 },
  reporter: "list",
  webServer: {
    command:
      "cd ../backend && ../.venv/bin/python -m tests.acceptance.browser_server",
    url: "http://127.0.0.1:5191/__acceptance__/health",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5191",
    viewport: { width: 1440, height: 900 },
    storageState: { cookies: [], origins: [] },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
})
