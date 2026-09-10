import { defineConfig } from "@playwright/test"
import foundation from "./playwright.upload-transfer.config"

export default defineConfig({
  ...foundation,
  testMatch: ["materials.spec.ts", "r2-ingest.spec.ts"],
  timeout: 60000,
  use: { ...foundation.use, baseURL: "http://127.0.0.1:5198" },
  webServer: {
    command:
      "node ../node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5198 --strictPort",
    url: "http://127.0.0.1:5198",
    reuseExistingServer: false,
  },
})
