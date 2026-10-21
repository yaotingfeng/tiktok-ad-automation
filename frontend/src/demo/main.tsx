import { installDemoIsolation } from "./runtime/isolation"

// 体验版入口。顺序很重要：先隔离存储和网络，再动态加载应用代码，
// 保证任何模块在初始化时都读不到生产凭证、也发不出真实请求。
const isolated = installDemoIsolation()

function renderFailure(error: unknown) {
  console.error("[demo] boot failed", error)
  const root = document.getElementById("root")
  if (!root) return
  root.innerHTML = ""
  const box = document.createElement("div")
  box.setAttribute("role", "alert")
  box.style.cssText =
    "max-width:420px;margin:20vh auto;padding:24px;font-family:system-ui,sans-serif;border:1px solid #e5e5e5;border-radius:12px;text-align:center"
  box.innerHTML =
    '<h1 style="font-size:18px;margin:0 0 8px">体验版加载失败</h1><p style="color:#666;font-size:14px;margin:0">请刷新页面重试。体验版不会连接任何真实数据。</p>'
  root.append(box)
}

async function boot() {
  await isolated
  const [{ client }, { createHashHistory }, app, adapter, navigation, shell] =
    await Promise.all([
      import("@/client/client.gen"),
      import("@tanstack/react-router"),
      import("@/app"),
      import("./runtime/adapter"),
      import("./runtime/navigation"),
      import("./shell/DemoShell"),
      import("./handlers"),
    ])
  client.setConfig({
    baseURL: adapter.DEMO_BASE_URL,
    adapter: adapter.demoAdapter,
    auth: () => localStorage.getItem("access_token") || "",
  })
  const router = app.createAppRouter(createHashHistory())
  navigation.installDemoNavigation((path, options) => {
    router.history.push(path, undefined, {
      ignoreBlocker: options?.leavePage ?? false,
    })
    // 真实系统这里是整页跳转（例如从 TikTok 授权页返回），页面状态和弹窗都会重置；
    // 体验版不能刷新（演示数据在内存里），改为重新挂载页面达到同样效果。
    if (options?.leavePage) shell.reloadDemoPage()
  })
  const queryClient = app.createAppQueryClient()
  app.renderApp({
    router,
    queryClient,
    overlay: <shell.DemoShell router={router} queryClient={queryClient} />,
    wrapRoutes: (routes) => <shell.Remountable>{routes}</shell.Remountable>,
  })
  document.documentElement.dataset.demoReady = "true"
}

boot().catch(renderFailure)
