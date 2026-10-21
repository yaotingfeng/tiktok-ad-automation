import { defineConfig, devices } from "@playwright/test"

// 体验版端到端检查：对体验版构建产物（vite preview，/demo/）跑主线引导与逐页巡检，
// 断言全程没有任何请求发往 /api、没有未覆盖操作。与工作台用例分开，避免共享开发服务器。
const port = process.env.PLAYWRIGHT_DEMO_PORT ?? "5181"
const baseURL = `http://localhost:${port}`

export default defineConfig({
  testDir: "./tests-demo",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "list" : "html",
  timeout: 120_000,
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  webServer: {
    command: `bun run preview:demo --port ${port} --strictPort`,
    url: `${baseURL}/demo/`,
    reuseExistingServer: !process.env.CI,
  },
})
