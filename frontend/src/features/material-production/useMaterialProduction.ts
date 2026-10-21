import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { MaterialProductionService } from "@/client"
import { errorMessage } from "@/features/tenants/shared"

export function useMaterialProduction(tenantId?: string) {
  const [stage, setStage] = useState<"idle" | "requesting" | "redirecting">(
    "idle",
  )
  const controller = useRef<AbortController | null>(null)
  const loadingToast = useRef<string | number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const currentTenant = useRef(tenantId)
  currentTenant.current = tenantId

  useEffect(() => {
    currentTenant.current = tenantId
    const reset = () => {
      controller.current?.abort()
      controller.current = null
      if (timer.current !== null) clearTimeout(timer.current)
      timer.current = null
      if (loadingToast.current !== null) toast.dismiss(loadingToast.current)
      loadingToast.current = null
    }
    setStage("idle")
    // 从素材工具后退时，浏览器可能恢复整个页面快照，需要解除旧的跳转状态。
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        reset()
        setStage("idle")
      }
    }
    window.addEventListener("pageshow", onPageShow)
    return () => {
      reset()
      window.removeEventListener("pageshow", onPageShow)
    }
  }, [tenantId])

  const open = async () => {
    if (!tenantId || controller.current) return
    const request = new AbortController()
    controller.current = request
    setStage("requesting")
    const toastId = toast.loading("正在登录素材工作台…", {
      description: "登录成功后将自动跳转，请稍候。",
    })
    loadingToast.current = toastId
    let timedOut = false
    let navigating = false
    timer.current = setTimeout(() => {
      timedOut = true
      request.abort()
    }, 25_000)
    try {
      // 仅点击时签发；不通过 query/effect 预取，不缓存或打印临时链接。
      const { data } =
        await MaterialProductionService.productionPostMaterialLoginLink({
          path: { tenant_id: tenantId },
          body: {},
          signal: request.signal,
        })
      if (request.signal.aborted || currentTenant.current !== tenantId) return
      const target = new URL(data.login_url)
      if (
        target.origin !== "https://manjumt.gzjunbo.net" ||
        target.pathname !== "/api/login-links/consume" ||
        target.username ||
        target.password ||
        target.hash
      ) {
        throw new Error("Invalid material login destination")
      }
      setStage("redirecting")
      toast.loading("正在打开素材工作台…", {
        id: toastId,
        description: "登录验证已完成，正在跳转。",
      })
      // 跨站导航会暂停旧页更新，先让提示组件提交本轮渲染。
      await new Promise<void>((resolve) => setTimeout(resolve, 0))
      if (request.signal.aborted || currentTenant.current !== tenantId) return
      window.location.assign(target.href)
      // assign 返回时新页面尚未加载，继续保留提示和防重复点击状态。
      navigating = true
    } catch (error) {
      if (
        controller.current === request &&
        currentTenant.current === tenantId
      ) {
        toast.error(
          timedOut ? "登录素材工作台超时，请稍后重试" : errorMessage(error),
          {
            id: toastId,
            description: undefined,
          },
        )
        loadingToast.current = null
      }
    } finally {
      if (controller.current === request) {
        if (timer.current !== null) clearTimeout(timer.current)
        timer.current = null
        if (!navigating) {
          controller.current = null
          setStage("idle")
        }
      }
    }
  }
  return {
    open: () => void open(),
    pending: stage !== "idle",
    pendingLabel: stage === "redirecting" ? "正在打开…" : "正在登录…",
  }
}
