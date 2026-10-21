// 体验版的异步任务按真实时间推进：每个阶段固定几秒，刷新页面即重置。
export const now = () => Date.now()
export const isoAt = (ms: number) => new Date(ms).toISOString()
export const isoNow = () => isoAt(now())

const DAY = 24 * 60 * 60 * 1000
const BEIJING_OFFSET = 8 * 60 * 60 * 1000

/** 相对今天的北京时间日期，例如 dateDaysAgo(1) 是昨天，格式 YYYY-MM-DD。 */
export function dateDaysAgo(days: number) {
  return new Date(now() - days * DAY + BEIJING_OFFSET)
    .toISOString()
    .slice(0, 10)
}

/** 相对现在若干天、指定北京时间小时的 ISO 时间。 */
export function isoDaysAgo(days: number, hour = 10, minute = 0) {
  const date = dateDaysAgo(days)
  const ms =
    Date.parse(`${date}T00:00:00Z`) -
    BEIJING_OFFSET +
    (hour * 60 + minute) * 60 * 1000
  return isoAt(ms)
}

/**
 * 根据开始时间返回当前阶段。stages 为 [状态, 持续毫秒] 列表，最后一个是终态（持续时间忽略）。
 * progress 为 0–1 的整体进度。
 */
export function stageAt<S extends string>(
  startedAt: number,
  stages: readonly (readonly [S, number])[],
): { stage: S; index: number; progress: number; done: boolean } {
  let elapsed = now() - startedAt
  const last = stages.length - 1
  for (let index = 0; index < last; index++) {
    const [stage, duration] = stages[index]
    if (elapsed < duration)
      return {
        stage,
        index,
        progress: (index + elapsed / duration) / last,
        done: false,
      }
    elapsed -= duration
  }
  return { stage: stages[last][0], index: last, progress: 1, done: true }
}

let seq = 5000
/** 体验版内自增整数 ID。 */
export const nextId = () => ++seq

/** 生成符合 UUID v4 格式的 ID，便于路由参数校验；同一 n 总得到同一个 ID。 */
export function demoUuid(n: number = nextId()) {
  const tail = n.toString(16).padStart(12, "0").slice(-12)
  return `d0d0d0d0-0000-4000-8000-${tail}`
}

/** 演示用的数字 ID（TikTok 风格 19 位）。 */
export function demoNumericId(n: number = nextId(), prefix = "7431") {
  return `${prefix}${n.toString().padStart(15, "0")}`.slice(0, 19)
}
