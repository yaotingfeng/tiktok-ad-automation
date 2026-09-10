import { defineConfig, devices } from "@playwright/test"

export default defineConfig({
  testDir: "./tests",
  testMatch: "upload-foundation.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5197",
    storageState: { cookies: [], origins: [] },
  },
  webServer: {
    command:
      "node ../node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5197 --strictPort",
    url: "http://127.0.0.1:5197/tests/harness/upload-transfer.html",
    reuseExistingServer: false,
  },
})
