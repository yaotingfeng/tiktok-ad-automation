import {
  DEMO_OPERATIONS,
  type DemoOperationId,
  type DemoOperationTypes,
} from "../operations.gen"

// 体验版假后端的处理函数注册表：按 openapi operationId 注册，覆盖检查脚本据此比对接口。
type OpData<Id extends DemoOperationId> = DemoOperationTypes[Id]["data"]
type Field<T, K extends string> = T extends { [P in K]?: infer V } ? V : never

export interface DemoRequest<Id extends DemoOperationId = DemoOperationId> {
  operationId: Id
  method: string
  path: NonNullable<Field<OpData<Id>, "path">>
  query: NonNullable<Field<OpData<Id>, "query">>
  body: Field<OpData<Id>, "body">
  /** 未解析的查询参数，用于数组等特殊格式。 */
  search: URLSearchParams
  headers: Record<string, string>
}

/** 需要非 200 状态码或自定义响应时返回它。 */
export class DemoReply {
  constructor(
    readonly status: number,
    readonly body: unknown,
    readonly headers: Record<string, string> = {},
  ) {}
}

/** 业务错误：与后端 DomainError 的响应体保持一致。 */
export function demoError(
  status: number,
  code: string,
  message: string,
  extra: Record<string, unknown> = {},
) {
  return new DemoReply(status, {
    code,
    message,
    retryable: false,
    ...extra,
  })
}

type DemoResult<Id extends DemoOperationId> =
  | DemoOperationTypes[Id]["response"]
  | DemoReply
type DemoHandler<Id extends DemoOperationId> = (
  req: DemoRequest<Id>,
) => DemoResult<Id> | Promise<DemoResult<Id>>

// 注册表内部按 operationId 擦除类型。
const handlers = new Map<DemoOperationId, DemoHandler<any>>()

export function handle<Id extends DemoOperationId>(
  operationId: Id,
  handler: DemoHandler<Id>,
) {
  if (handlers.has(operationId))
    throw new Error(`demo handler registered twice: ${operationId}`)
  handlers.set(operationId, handler)
}

interface CompiledRoute {
  id: DemoOperationId
  method: string
  pattern: RegExp
  names: string[]
  literalLength: number
  queryKinds: Record<string, string>
}

const routes: CompiledRoute[] = Object.entries(DEMO_OPERATIONS)
  .map(([id, op]) => {
    const names: string[] = []
    const source = op.path
      .replace(/[.*+?^$()|[\]\\]/g, "\\$&")
      .replace(/\{([^}]+)\}/g, (_, name: string) => {
        names.push(name)
        return "([^/]+)"
      })
    return {
      id: id as DemoOperationId,
      method: op.method,
      pattern: new RegExp(`^${source}$`),
      names,
      literalLength: op.path.replace(/\{[^}]+\}/g, "").length,
      queryKinds: op.query as Record<string, string>,
    }
  })
  // 字面量越长越优先，避免 /items/{id} 抢走 /items/summary。
  .sort((a, b) => b.literalLength - a.literalLength)

export function resolveDemoRoute(method: string, pathname: string) {
  for (const route of routes) {
    if (route.method !== method) continue
    const match = route.pattern.exec(pathname)
    if (!match) continue
    const params: Record<string, string> = {}
    route.names.forEach((name, index) => {
      params[name] = decodeURIComponent(match[index + 1])
    })
    return {
      id: route.id,
      params,
      queryKinds: route.queryKinds,
      handler: handlers.get(route.id),
    }
  }
  return null
}

export function registeredDemoOperations() {
  return [...handlers.keys()]
}
