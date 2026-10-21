import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { Button } from "@/components/ui/button"
import { resolveTarget } from "./find"
import type { Tour, TourContext } from "./types"

// 引导浮层：四块半透明遮罩围出高亮区域（遮罩不拦截点击，演示者可以随意操作页面），
// 旁边是说明卡片。高亮元素按页面实时位置跟随，弹窗打开、列表加载完成后自动找到。
const CARD_WIDTH = 340
const GAP = 12

interface Props {
  tour: Tour
  index: number
  context: TourContext
  navigate: (path: string) => void
  onNext: () => void
  onPrev: () => void
  onClose: () => void
}

export function TourOverlay({
  tour,
  index,
  context,
  navigate,
  onNext,
  onPrev,
  onClose,
}: Props) {
  const step = tour.steps[index]
  const [element, setElement] = useState<Element | null>(null)
  const [rect, setRect] = useState<DOMRect | null>(null)
  const [searching, setSearching] = useState(true)
  const cardRef = useRef<HTMLDivElement>(null)
  const contextRef = useRef(context)
  contextRef.current = context

  // 进入步骤：必要时跳转页面，然后轮询查找高亮元素。
  useEffect(() => {
    setElement(null)
    setRect(null)
    setSearching(true)
    const path =
      typeof step.path === "function"
        ? step.path(contextRef.current)
        : step.path
    if (path) {
      const [targetPath] = path.split("?")
      if (targetPath !== contextRef.current.pathname || path.includes("?"))
        navigate(path)
    }
    if (!step.target) {
      setSearching(false)
      return
    }
    let found: Element | null = null
    const started = Date.now()
    const timer = window.setInterval(() => {
      found = resolveTarget(step.target, contextRef.current)
      if (found) {
        window.clearInterval(timer)
        setElement(found)
        setSearching(false)
        found.scrollIntoView({ block: "center", inline: "nearest" })
      } else if (Date.now() - started > 8000) {
        window.clearInterval(timer)
        setSearching(false)
      }
    }, 150)
    return () => window.clearInterval(timer)
  }, [step, navigate])

  // 高亮元素位置随滚动、布局变化实时更新；元素被卸载时重新查找。
  useEffect(() => {
    if (!element) return
    let frame = 0
    const tick = () => {
      if (!element.isConnected) {
        const again = resolveTarget(step.target, contextRef.current)
        if (again) setElement(again)
        else setRect(null)
      } else {
        const next = element.getBoundingClientRect()
        setRect((prev) =>
          prev &&
          prev.x === next.x &&
          prev.y === next.y &&
          prev.width === next.width &&
          prev.height === next.height
            ? prev
            : next,
        )
      }
      frame = window.requestAnimationFrame(tick)
    }
    frame = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame)
  }, [element, step])

  // 标记当前高亮元素，便于端到端测试按引导逐步点击。
  useEffect(() => {
    if (!element) return
    element.setAttribute("data-demo-tour-target", step.advance ?? "next")
    return () => element.removeAttribute("data-demo-tour-target")
  }, [element, step])

  // “点击高亮元素继续”：捕获阶段监听，不影响页面自身的点击处理。
  // 点击常常触发页面跳转，外层会重新渲染并传入新的 onNext；用 ref 保存最新回调，
  // 且计时器一旦启动就不随重新渲染取消，否则跳转类步骤永远不会前进。
  const onNextRef = useRef(onNext)
  onNextRef.current = onNext
  const advanced = useRef(false)
  useEffect(() => {
    if (step.advance !== "click" || !element) return
    const onClick = (event: MouseEvent) => {
      if (advanced.current) return
      const current = element.isConnected
        ? element
        : resolveTarget(step.target, contextRef.current)
      if (current?.contains(event.target as Node)) {
        advanced.current = true
        window.setTimeout(() => onNextRef.current(), step.settleMs ?? 500)
      }
    }
    document.addEventListener("click", onClick, true)
    return () => document.removeEventListener("click", onClick, true)
  }, [step, element])

  // 页面里打开的弹窗会把外部点击视为“点到外面”而关闭，卡片上的按下事件不向外传播。
  useEffect(() => {
    const card = cardRef.current
    if (!card) return
    const stop = (event: Event) => event.stopPropagation()
    card.addEventListener("pointerdown", stop)
    card.addEventListener("mousedown", stop)
    card.addEventListener("focusin", stop)
    return () => {
      card.removeEventListener("pointerdown", stop)
      card.removeEventListener("mousedown", stop)
      card.removeEventListener("focusin", stop)
    }
  })

  // readyWhen：轮询页面状态，就绪前禁用“下一步”。
  const [ready, setReady] = useState(!step.readyWhen)
  useEffect(() => {
    if (!step.readyWhen) return
    const started = Date.now()
    const timer = window.setInterval(() => {
      let ok = false
      try {
        ok = step.readyWhen?.() ?? true
      } catch {
        ok = false
      }
      if (ok || Date.now() - started > 20_000) {
        setReady(true)
        window.clearInterval(timer)
      }
    }, 300)
    return () => window.clearInterval(timer)
  }, [step])

  const [cardHeight, setCardHeight] = useState(180)
  useLayoutEffect(() => {
    if (cardRef.current) setCardHeight(cardRef.current.offsetHeight)
  })

  const position = cardPosition(rect, cardHeight, step.placement)
  const last = index === tour.steps.length - 1
  const pad = 6
  const hole = rect && {
    left: Math.max(0, rect.left - pad),
    top: Math.max(0, rect.top - pad),
    right: Math.min(window.innerWidth, rect.right + pad),
    bottom: Math.min(window.innerHeight, rect.bottom + pad),
  }
  const dim =
    "fixed bg-black/35 pointer-events-none transition-all duration-150"

  return createPortal(
    <div data-demo-shell className="z-[2147483000]">
      {hole ? (
        <>
          <div
            className={dim}
            style={{
              left: 0,
              top: 0,
              width: "100vw",
              height: hole.top,
              zIndex: 2147483000,
            }}
          />
          <div
            className={dim}
            style={{
              left: 0,
              top: hole.bottom,
              width: "100vw",
              bottom: 0,
              zIndex: 2147483000,
            }}
          />
          <div
            className={dim}
            style={{
              left: 0,
              top: hole.top,
              width: hole.left,
              height: hole.bottom - hole.top,
              zIndex: 2147483000,
            }}
          />
          <div
            className={dim}
            style={{
              left: hole.right,
              top: hole.top,
              right: 0,
              height: hole.bottom - hole.top,
              zIndex: 2147483000,
            }}
          />
          <div
            className="pointer-events-none fixed rounded-md ring-2 ring-amber-400 transition-all duration-150"
            style={{
              left: hole.left,
              top: hole.top,
              width: hole.right - hole.left,
              height: hole.bottom - hole.top,
              zIndex: 2147483001,
              boxShadow: "0 0 0 4px rgba(251,191,36,0.25)",
            }}
          />
        </>
      ) : null}
      <div
        ref={cardRef}
        role="dialog"
        aria-label={`${tour.title}：${step.title}`}
        data-demo-tour-card
        className="pointer-events-auto fixed rounded-xl border bg-background p-4 text-sm shadow-2xl"
        style={{
          width: CARD_WIDTH,
          maxWidth: "calc(100vw - 24px)",
          left: position.left,
          top: position.top,
          zIndex: 2147483002,
        }}
      >
        <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {tour.title} · {index + 1}/{tour.steps.length}
          </span>
          <button
            type="button"
            className="rounded px-1 hover:bg-muted"
            onClick={onClose}
            aria-label="退出引导"
          >
            退出
          </button>
        </div>
        <h2 className="mb-1 text-base font-semibold">{step.title}</h2>
        <p className="whitespace-pre-line leading-relaxed text-muted-foreground">
          {step.body}
        </p>
        {step.advance === "click" && element ? (
          <p className="mt-2 rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-200">
            点击高亮的位置继续，或点“下一步”跳过。
          </p>
        ) : null}
        {step.target && !element && searching ? (
          <p className="mt-2 text-xs text-muted-foreground">
            正在定位页面元素…
          </p>
        ) : null}
        {step.action ? (
          <Button
            size="sm"
            variant="outline"
            className="mt-3 w-full border-amber-300"
            onClick={() => void step.action?.run(contextRef.current)}
          >
            {step.action.label}
          </Button>
        ) : null}
        <div className="mt-3 flex items-center justify-between gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={onPrev}
            disabled={index === 0}
          >
            上一步
          </Button>
          <Button size="sm" disabled={!ready} onClick={last ? onClose : onNext}>
            {ready ? (last ? "完成" : "下一步") : "处理中…"}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function cardPosition(
  rect: DOMRect | null,
  height: number,
  placement: Props["tour"]["steps"][number]["placement"],
) {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const width = Math.min(CARD_WIDTH, vw - 24)
  const clampX = (x: number) => Math.min(Math.max(12, x), vw - width - 12)
  const clampY = (y: number) => Math.min(Math.max(12, y), vh - height - 12)
  // 高亮的是整块页面内容时，卡片固定在右下角，不遮挡左上角的体验版标识和侧栏。
  if (rect && rect.width * rect.height > vw * vh * 0.4)
    return { left: vw - width - 24, top: clampY(vh - height - 24) }
  if (!rect)
    return { left: clampX((vw - width) / 2), top: clampY(vh - height - 96) }
  const below = vh - rect.bottom
  const above = rect.top
  const right = vw - rect.right
  const prefer =
    placement ??
    (below >= height + GAP
      ? "bottom"
      : above >= height + GAP
        ? "top"
        : right >= width + GAP
          ? "right"
          : "left")
  switch (prefer) {
    case "top":
      return { left: clampX(rect.left), top: clampY(rect.top - height - GAP) }
    case "right":
      return { left: clampX(rect.right + GAP), top: clampY(rect.top) }
    case "left":
      return { left: clampX(rect.left - width - GAP), top: clampY(rect.top) }
    default:
      return { left: clampX(rect.left), top: clampY(rect.bottom + GAP) }
  }
}
