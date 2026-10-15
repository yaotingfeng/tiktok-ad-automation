import { defineConfig, devices } from "@playwright/test"

const port = process.env.R2_ACCEPTANCE_PORT ?? "5293"

export default defineConfig({
  testDir: "./tests",
  testMatch: "acceptance-r2.spec.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  webServer: {
    command:
      "bun run build && cd ../backend && PYTHONPATH=. exec ../.venv/bin/python -m tests.acceptance.r2_browser_server",
    url: `http://127.0.0.1:${port}/__r2_acceptance__/health`,
    reuseExistingServer: false,
    env: { VITE_API_URL: "" },
    timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
  },
  use: {
    ...devices["Desktop Chrome"],
    baseURL: `http://127.0.0.1:${port}`,
    ignoreHTTPSErrors: true, // Only the synthetic loopback storage certificate.
    viewport: { width: 1440, height: 900 },
    storageState: { cookies: [], origins: [] },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
})
