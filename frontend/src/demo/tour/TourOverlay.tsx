import { MousePointerClick } from "lucide-react"
import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { Button } from "@/components/ui/button"
import { resolveTarget } from "./find"
import type { Tour, TourContext } from "./types"

// 引导浮层：四块半透明遮罩围出高亮区域（遮罩不拦截点击，体验者直接点页面上的真实按钮），
// 旁边是说明卡片。每一步都对应页面上一个具体元素：
// - 需要点击的步骤高亮闪烁，卡片写明要点哪个按钮，点中后自动进入下一步；
// - 说明类步骤看完点“下一步”；
// - 找不到元素（比如弹窗被关掉、走到了别的页面）时给出“回到上一步 / 跳过”。
const CARD_WIDTH = 360
const GAP = 14
const FIND_TIMEOUT = 6000
const READY_TIMEOUT = 25_000

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
  const [missing, setMissing] = useState(false)
  const cardRef = useRef<HTMLDivElement>(null)
  const contextRef = useRef(context)
  contextRef.current = context

  // 进入步骤：不在该步骤的页面就先跳过去，然后轮询查找高亮元素。
  useEffect(() => {
    setElement(null)
    setRect(null)
    setMissing(false)
    const path =
      typeof step.path === "function"
        ? step.path(contextRef.current)
        : step.path
    if (path && path.split("?")[0] !== contextRef.current.pathname)
      navigate(path)
    if (!step.target) return
    const started = Date.now()
    const timer = window.setInterval(() => {
      const found = resolveTarget(step.target, contextRef.current)
      if (found) {
        window.clearInterval(timer)
        setElement(found)
        found.scrollIntoView({ block: "center", inline: "nearest" })
      } else if (Date.now() - started > FIND_TIMEOUT) {
        window.clearInterval(timer)
        setMissing(true)
      }
    }, 150)
    return () => window.clearInterval(timer)
  }, [step, navigate])

  // 高亮元素位置随滚动、布局变化实时更新；元素被卸载（列表刷新）时重新查找。
  useEffect(() => {
    if (!element) return
    let frame = 0
    let current = element
    const tick = () => {
      if (!current.isConnected) {
        const again = resolveTarget(step.target, contextRef.current)
        if (again) {
          current = again
          setElement(again)
        } else setRect(null)
      } else {
        const next = current.getBoundingClientRect()
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

  // 点击高亮元素后自动前进：捕获阶段监听，不影响页面自身的点击处理。
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

  // readyWhen：轮询页面状态，就绪前“下一步”显示为等待中。
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
      if (ok || Date.now() - started > READY_TIMEOUT) {
        setReady(true)
        window.clearInterval(timer)
      }
    }, 300)
    return () => window.clearInterval(timer)
  }, [step])

  const [cardHeight, setCardHeight] = useState(200)
  useLayoutEffect(() => {
    if (cardRef.current) setCardHeight(cardRef.current.offsetHeight)
  })

  const position = cardPosition(rect, cardHeight, step.placement)
  const last = index === tour.steps.length - 1
  const clickStep = step.advance === "click"
  const chapterSteps = tour.steps.filter((s) => s.chapter === step.chapter)
  const chapterIndex = chapterSteps.indexOf(step) + 1
  const pad = 6
  const hole = rect && {
    left: Math.max(0, rect.left - pad),
    top: Math.max(0, rect.top - pad),
    right: Math.min(window.innerWidth, rect.right + pad),
    bottom: Math.min(window.innerHeight, rect.bottom + pad),
  }
  const dim = "fixed bg-black/40 pointer-events-none"

  return createPortal(
    <div data-demo-shell>
      {hole ? (
        <>
          {[
            { left: 0, top: 0, width: "100vw", height: hole.top },
            { left: 0, top: hole.bottom, width: "100vw", bottom: 0 },
            {
              left: 0,
              top: hole.top,
              width: hole.left,
              height: hole.bottom - hole.top,
            },
            {
              left: hole.right,
              top: hole.top,
              right: 0,
              height: hole.bottom - hole.top,
            },
          ].map((style, i) => (
            <div
              key={i}
              className={dim}
              style={{ ...style, zIndex: 2147483000 }}
            />
          ))}
          <div
            className={`pointer-events-none fixed rounded-md ring-[3px] ring-amber-400 ${clickStep ? "animate-pulse" : ""}`}
            style={{
              left: hole.left,
              top: hole.top,
              width: hole.right - hole.left,
              height: hole.bottom - hole.top,
              zIndex: 2147483001,
              boxShadow: "0 0 0 6px rgba(251,191,36,0.35)",
            }}
          />
        </>
      ) : null}
      <div
        ref={cardRef}
        role="dialog"
        aria-label={`${tour.title}：${step.title}`}
        data-demo-tour-card
        className="pointer-events-auto fixed rounded-xl border-2 border-amber-300 bg-background p-4 text-sm shadow-2xl"
        style={{
          width: CARD_WIDTH,
          maxWidth: "calc(100vw - 24px)",
          left: position.left,
          top: position.top,
          zIndex: 2147483002,
        }}
      >
        <div className="mb-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
          <span className="truncate">
            {step.chapter
              ? `${step.chapter} · ${chapterIndex}/${chapterSteps.length}`
              : tour.title}
            <span className="ml-2 tabular-nums" data-demo-tour-progress>
              总进度 {index + 1}/{tour.steps.length}
            </span>
          </span>
          <button
            type="button"
            className="shrink-0 rounded px-1 hover:bg-muted"
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
        {clickStep && !missing ? (
          <p className="mt-3 flex items-center gap-2 rounded-md bg-amber-100 px-3 py-2 font-medium text-amber-900 dark:bg-amber-900 dark:text-amber-50">
            <MousePointerClick className="size-4 shrink-0" aria-hidden />
            {element
              ? `请点击高亮的「${step.cta ?? step.title}」`
              : "正在定位要点击的位置…"}
          </p>
        ) : null}
        {!clickStep && step.target && !element && !missing ? (
          <p className="mt-2 text-xs text-muted-foreground">
            正在定位页面元素…
          </p>
        ) : null}
        {missing ? (
          <div className="mt-3 rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
            页面上没有找到这一步要看的位置，可能弹窗已关闭或页面已切换。可以回到上一步重新操作，或跳过这一步。
          </div>
        ) : null}
        {!ready ? (
          <p className="mt-2 text-xs text-muted-foreground" role="status">
            {step.waitingText ?? "正在处理，请稍候…"}
          </p>
        ) : null}
        {step.action ? (
          <Button
            size="sm"
            variant="outline"
            className="mt-3 w-full border-amber-400 bg-amber-50 hover:bg-amber-100 dark:bg-amber-950"
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
          {clickStep && !missing ? (
            <Button size="sm" variant="ghost" onClick={onNext}>
              跳过这步
            </Button>
          ) : (
            <Button
              size="sm"
              disabled={!ready && !missing}
              onClick={last ? onClose : onNext}
            >
              {missing ? "跳过" : !ready ? "处理中…" : last ? "完成" : "下一步"}
            </Button>
          )}
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
  if (!rect)
    return { left: clampX((vw - width) / 2), top: clampY((vh - height) / 2) }
  // 高亮的是大块区域（整张表、弹窗）时，卡片贴在右下角，不挡住内容。
  if (rect.width * rect.height > vw * vh * 0.35)
    return { left: vw - width - 24, top: clampY(vh - height - 24) }
  const below = vh - rect.bottom
  const above = rect.top
  const right = vw - rect.right
  const left = rect.left
  const prefer =
    placement ??
    (below >= height + GAP
      ? "bottom"
      : above >= height + GAP
        ? "top"
        : right >= width + GAP
          ? "right"
          : left >= width + GAP
            ? "left"
            : "bottom")
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
