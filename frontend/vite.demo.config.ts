import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import react from "@vitejs/plugin-react-swc"
import { defineConfig } from "vite"

// 体验版构建：同一套页面，入口换成 demo/index.html → src/demo/main.tsx（浏览器内假后端），
// 产物写入生产产物目录的 demo/ 子目录，由后端以 /demo/ 提供。生产入口不引用任何体验版代码。
// root 保持在 frontend/，Tailwind 才会扫描 src/ 下的样式类；生产构建已先清空产物目录，这里不再清空。
const frontendDir = import.meta.dirname
export default defineConfig({
  root: frontendDir,
  envDir: frontendDir,
  // 品牌图标等静态文件与生产共用同一份（生产产物目录里本来就有），本地预览时也能显示。
  publicDir: path.resolve(frontendDir, "public"),
  server: { port: 5180 },
  preview: { port: 5181 },
  build: {
    // 本地并行验证时可用 DEMO_OUT_DIR 指定独立目录，互不覆盖；发布构建使用默认目录。
    outDir:
      process.env.DEMO_OUT_DIR ??
      path.resolve(frontendDir, "../backend/app/frontend"),
    emptyOutDir: false,
    assetsDir: "demo/assets",
    rolldownOptions: {
      input: path.resolve(frontendDir, "demo/index.html"),
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(frontendDir, "./src"),
    },
  },
  plugins: [
    tanstackRouter({
      target: "react",
      autoCodeSplitting: true,
    }),
    react(),
    tailwindcss(),
  ],
})
