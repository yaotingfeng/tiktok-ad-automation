import {
  type AxiosAdapter,
  AxiosError,
  AxiosHeaders,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios"
import { reportDemoGap } from "./gaps"
import { DemoReply, type DemoRequest, resolveDemoRoute } from "./registry"

// 体验版的 axios 适配器：生成客户端的所有请求都在这里就地应答，永远不发到网络。
export const DEMO_BASE_URL = "demo://tk-ada"

const demoRequestLog: string[] = []
;(window as unknown as { __tkAdaDemoLog: string[] }).__tkAdaDemoLog =
  demoRequestLog

const latency = () => 120 + Math.random() * 220
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function parseBody(data: unknown): unknown {
  if (data == null) return undefined
  if (typeof data === "string") {
    try {
      return JSON.parse(data)
    } catch {
      return Object.fromEntries(new URLSearchParams(data))
    }
  }
  if (data instanceof URLSearchParams) return Object.fromEntries(data)
  if (data instanceof FormData) {
    const out: Record<string, FormDataEntryValue | FormDataEntryValue[]> = {}
    data.forEach((value, key) => {
      const prev = out[key]
      out[key] =
        prev === undefined
          ? value
          : Array.isArray(prev)
            ? [...prev, value]
            : [prev, value]
    })
    return out
  }
  return data
}

// URL 查询参数都是字符串；按 openapi 声明的类型还原，处理函数拿到的值与生成类型一致。
function queryObject(search: URLSearchParams, kinds: Record<string, string>) {
  const out: Record<string, unknown> = {}
  for (const key of new Set(search.keys())) {
    const values = search.getAll(key)
    const kind = kinds[key]
    const convert = (value: string) =>
      kind === "boolean"
        ? value === "true"
        : kind === "number" && value !== "" && !Number.isNaN(Number(value))
          ? Number(value)
          : value
    out[key] =
      kind === "array"
        ? values.flatMap((value) => value.split(",")).filter(Boolean)
        : values.length > 1
          ? values.map(convert)
          : convert(values[0])
  }
  return out
}

function toResponse(
  config: InternalAxiosRequestConfig,
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): AxiosResponse {
  return {
    data: body,
    status,
    statusText: String(status),
    headers: new AxiosHeaders({
      "content-type": "application/json",
      ...headers,
    }),
    config,
    request: {},
  }
}

export const demoAdapter: AxiosAdapter = async (config) => {
  const raw = config.url ?? ""
  const url = new URL(raw.startsWith("demo:") ? raw : `${DEMO_BASE_URL}${raw}`)
  const search = new URLSearchParams(url.search)
  if (config.params && typeof config.params === "object") {
    for (const [key, value] of Object.entries(config.params)) {
      if (value === undefined || value === null) continue
      for (const item of Array.isArray(value) ? value : [value])
        search.append(key, String(item))
    }
  }
  const method = (config.method ?? "get").toUpperCase()
  await sleep(latency())

  const route = resolveDemoRoute(method, url.pathname)
  let status = 200
  let body: unknown
  let headers: Record<string, string> = {}
  if (!route?.handler) {
    reportDemoGap(`${method} ${url.pathname}`)
    status = 501
    body = {
      code: "demo_not_covered",
      message: "体验版暂未覆盖这个操作",
      retryable: false,
    }
  } else {
    const request: DemoRequest = {
      operationId: route.id,
      method,
      path: route.params as never,
      query: queryObject(search, route.queryKinds) as never,
      body: parseBody(config.data) as never,
      search,
      headers: Object.fromEntries(
        Object.entries(config.headers ?? {}).map(([k, v]) => [k, String(v)]),
      ),
    }
    try {
      const result = await route.handler(request)
      if (result instanceof DemoReply) {
        status = result.status
        body = result.body
        headers = result.headers
      } else {
        body = result ?? null
        if (body === null && method === "DELETE") status = 204
      }
    } catch (error) {
      console.error("[demo] handler failed", route.id, error)
      reportDemoGap(`${route.id}: ${(error as Error).message}`)
      status = 500
      body = {
        code: "internal_error",
        message: "体验版数据处理出错",
        retryable: false,
      }
    }
  }

  // 下载类接口返回 Blob 时按调用方期望的类型交付。
  if (config.responseType === "blob" && !(body instanceof Blob))
    body = new Blob([typeof body === "string" ? body : JSON.stringify(body)])

  // 调试用请求流水（只在内存里，便于端到端测试和排查体验版流程）。
  demoRequestLog.push(`${status} ${route?.id ?? `${method} ${url.pathname}`}`)
  if (demoRequestLog.length > 500) demoRequestLog.shift()
  const response = toResponse(config, status, body, headers)
  const validate = config.validateStatus ?? ((s: number) => s >= 200 && s < 300)
  if (validate(status)) return response
  throw new AxiosError(
    `Request failed with status code ${status}`,
    status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST,
    config,
    {},
    response,
  )
}
