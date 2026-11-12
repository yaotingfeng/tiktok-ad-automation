import { isAxiosError } from "axios"
import { client } from "@/client/client.gen"

/** 与现有业务客户端共享 VITE_API_URL、动态 Bearer 和错误处理，不另建认证来源。 */
export async function aiRequest<T>(
  url: string,
  options: {
    method?: "GET" | "POST" | "PATCH"
    body?: unknown
    signal?: AbortSignal
  } = {},
): Promise<T> {
  const response = await client.request<{ 200: T }, unknown, true>({
    url,
    method: options.method ?? "GET",
    body: options.body,
    signal: options.signal,
    responseType: "json",
    security: [{ scheme: "bearer", type: "http" }],
    throwOnError: true,
    ...(options.body === undefined
      ? {}
      : { headers: { "Content-Type": "application/json" } }),
  })
  return response.data
}

export function errorMessage(error: unknown): string {
  if (isAxiosError(error)) {
    const body = error.response?.data
    const detail = body?.detail ?? body?.error ?? body
    if (typeof detail === "string") return detail
    if (detail?.message) return String(detail.message)
    if (Array.isArray(detail))
      return detail.map((item) => item.msg ?? "输入无效").join("；")
  }
  return error instanceof Error ? error.message : "请求失败，请重试"
}

export function businessToday(timezone = "Asia/Shanghai") {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date())
}

/** 目录完整翻页，避免大租户只能选择第一页的账户/策略。 */
export async function allItems<T>(
  url: string,
  signal?: AbortSignal,
): Promise<T[]> {
  const result: T[] = []
  let cursor: string | null | undefined
  do {
    const separator = url.includes("?") ? "&" : "?"
    const page = await aiRequest<{ items: T[]; next_cursor?: string | null }>(
      `${url}${separator}limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    )
    result.push(...page.items)
    cursor = page.next_cursor
  } while (cursor)
  return result
}

/** 同一用户操作遇到网络失败时复用请求号，避免重试变成另一条创建/发布请求。 */
export function createRequestIdentity() {
  let previous: { digest: string; id: string } | undefined
  return (body: unknown) => {
    const digest = JSON.stringify(body)
    if (!previous || previous.digest !== digest)
      previous = { digest, id: crypto.randomUUID() }
    return previous.id
  }
}
