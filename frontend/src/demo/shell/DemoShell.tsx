import type { QueryClient } from "@tanstack/react-query"
import { BookOpen, Compass, RotateCcw, Sparkles } from "lucide-react"
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react"
import { createPortal } from "react-dom"
import { toast } from "sonner"
import type { AppRouter } from "@/app"
import { LoginService } from "@/client"
import { Button } from "@/components/ui/button"
import { MAIN_BC_ID, MAIN_TENANT_ID } from "../fixtures/world"
import { DEMO_GAP_EVENT } from "../runtime/gaps"
import { TourOverlay } from "../tour/TourOverlay"
import { MAIN_TOUR, pageTourFor } from "../tour/tours"
import type { Tour, TourContext } from "../tour/types"

// 体验版外壳：常驻“体验版 · 演示数据”标识、一键进入、主线引导、本页讲解、重置，
// 以及未覆盖操作的醒目提示。外壳渲染在应用路由之外，通过 router.history 读写位置。
export const DEMO_HOME = `/tenants/${MAIN_TENANT_ID}?bc_id=${MAIN_BC_ID}`

function useLocation(router: AppRouter) {
  const subscribe = useCallback(
    (callback: () => void) => router.history.subscribe(callback),
    [router],
  )
  const pathname = useSyncExternalStore(
    subscribe,
    () => router.history.location.pathname,
  )
  const search = useSyncExternalStore(
    subscribe,
    () => router.history.location.search,
  )
  return { pathname, search }
}

export function DemoShell({
  router,
  queryClient,
}: {
  router: AppRouter
  queryClient: QueryClient
}) {
  const { pathname, search } = useLocation(router)
  const [active, setActive] = useState<{ tour: Tour; index: number } | null>(
    null,
  )
  const [entering, setEntering] = useState(false)
  const navigate = useCallback(
    (path: string) => router.history.push(path),
    [router],
  )

  useEffect(() => {
    const onGap = (event: Event) =>
      toast.error("体验版暂未覆盖这个操作", {
        description: String((event as CustomEvent).detail),
      })
    window.addEventListener(DEMO_GAP_EVENT, onGap)
    return () => window.removeEventListener(DEMO_GAP_EVENT, onGap)
  }, [])

  // 浏览器标签页标题加上体验版标识，避免与生产版混淆。
  useEffect(() => {
    const mark = () => {
      if (!document.title.startsWith("【体验版】"))
        document.title = `【体验版】${document.title}`
    }
    mark()
    const head = document.querySelector("title")
    if (!head) return
    const observer = new MutationObserver(mark)
    observer.observe(head, {
      childList: true,
      characterData: true,
      subtree: true,
    })
    return () => observer.disconnect()
  }, [])

  const tenantId = /^\/tenants\/([^/]+)/.exec(pathname)?.[1] ?? MAIN_TENANT_ID
  const bcId = new URLSearchParams(search).get("bc_id") ?? MAIN_BC_ID
  const context: TourContext = useMemo(
    () => ({ tenantId, bcId, pathname }),
    [tenantId, bcId, pathname],
  )
  const onLogin = pathname === "/login"
  const pageTour = onLogin ? null : pageTourFor(pathname)

  const enterDemo = async (startTour: boolean) => {
    setEntering(true)
    try {
      const { data } = await LoginService.loginAccessToken({
        body: { username: "demo", password: "demo" },
      })
      localStorage.setItem("access_token", data.access_token)
      await queryClient.invalidateQueries({ queryKey: ["currentUser"] })
      navigate(DEMO_HOME)
      if (startTour) setActive({ tour: MAIN_TOUR, index: 0 })
    } finally {
      setEntering(false)
    }
  }

  const reset = () => {
    // 所有演示数据只在内存中，回到起点后整页刷新即恢复初始状态。
    window.location.hash = "#/login"
    window.location.reload()
  }

  const startTour = (tour: Tour) => {
    if (!localStorage.getItem("access_token") && tour === MAIN_TOUR) {
      void enterDemo(true)
      return
    }
    setActive({ tour, index: 0 })
  }

  return (
    <>
      {onLogin ? <LoginCard entering={entering} onEnter={enterDemo} /> : null}
      <Dock
        onStartMain={() => startTour(MAIN_TOUR)}
        pageTour={pageTour}
        onStartPage={(tour) => startTour(tour)}
        onReset={reset}
      />
      {active ? (
        <TourOverlay
          key={`${active.tour.id}:${active.index}`}
          tour={active.tour}
          index={active.index}
          context={context}
          navigate={navigate}
          onNext={() =>
            setActive((current) =>
              current && current.index < current.tour.steps.length - 1
                ? { ...current, index: current.index + 1 }
                : null,
            )
          }
          onPrev={() =>
            setActive((current) =>
              current
                ? { ...current, index: Math.max(0, current.index - 1) }
                : null,
            )
          }
          onClose={() => setActive(null)}
        />
      ) : null}
    </>
  )
}

function useStopOutsidePointer() {
  // 页面弹窗打开时，点击外壳不应被视为“点到弹窗外面”而关闭弹窗。
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const stop = (event: Event) => event.stopPropagation()
    el.addEventListener("pointerdown", stop)
    el.addEventListener("mousedown", stop)
    return () => {
      el.removeEventListener("pointerdown", stop)
      el.removeEventListener("mousedown", stop)
    }
  }, [])
  return ref
}

function Dock({
  onStartMain,
  pageTour,
  onStartPage,
  onReset,
}: {
  onStartMain: () => void
  pageTour: Tour | null
  onStartPage: (tour: Tour) => void
  onReset: () => void
}) {
  const ref = useStopOutsidePointer()
  return createPortal(
    <div
      ref={ref}
      data-demo-shell
      data-demo-dock
      // 覆盖在左上角品牌位置：不遮挡页面内容和弹窗底部的主操作按钮。
      className="pointer-events-auto fixed top-2 left-2 w-[256px] rounded-lg border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs shadow-sm dark:bg-amber-950"
      style={{ zIndex: 2147482000 }}
    >
      <p className="flex items-center gap-1 px-1 font-semibold text-amber-800 dark:text-amber-200">
        <Sparkles className="size-3.5" aria-hidden />
        TK-ADA 体验版 · 演示数据
      </p>
      <div className="mt-1 flex items-center gap-0.5">
        <Button
          size="sm"
          variant="ghost"
          className="h-7 px-2 text-xs"
          onClick={onStartMain}
        >
          <Compass aria-hidden />
          主线引导
        </Button>
        {pageTour ? (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs"
            onClick={() => onStartPage(pageTour)}
          >
            <BookOpen aria-hidden />
            本页怎么用
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          className="h-7 px-2 text-xs"
          onClick={onReset}
        >
          <RotateCcw aria-hidden />
          重置
        </Button>
      </div>
    </div>,
    document.body,
  )
}

function LoginCard({
  entering,
  onEnter,
}: {
  entering: boolean
  onEnter: (startTour: boolean) => void
}) {
  const ref = useStopOutsidePointer()
  return createPortal(
    <div
      ref={ref}
      data-demo-shell
      data-demo-login
      className="pointer-events-auto fixed top-6 left-1/2 w-[min(440px,calc(100vw-24px))] -translate-x-1/2 rounded-xl border border-amber-300 bg-background p-5 shadow-xl"
      style={{ zIndex: 2147482000 }}
    >
      <p className="mb-1 flex items-center gap-1 text-xs font-medium text-amber-700 dark:text-amber-300">
        <Sparkles className="size-3.5" aria-hidden />
        TK-ADA 体验版
      </p>
      <h1 className="mb-1 text-lg font-semibold">欢迎体验广告投放工具</h1>
      <p className="mb-4 text-sm leading-relaxed text-muted-foreground">
        这里的租户、账户、剧和报表都是演示数据，不连接任何真实账户，可以放心点击任何按钮。刷新或点“重置体验”即可回到初始状态。
      </p>
      <div className="flex flex-wrap gap-2">
        <Button disabled={entering} onClick={() => onEnter(true)}>
          一键进入并开始引导
        </Button>
        <Button
          variant="outline"
          disabled={entering}
          onClick={() => onEnter(false)}
        >
          自由体验
        </Button>
      </div>
    </div>,
    document.body,
  )
}
