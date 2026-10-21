// 从 openapi.json 生成体验版操作表：operationId → 方法、路径模板及生成客户端里的请求/响应类型。
// 用法：node scripts/generate-demo-operations.mjs（接口变化后与 generate-client 一起运行）。
import { readFileSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const root = join(dirname(fileURLToPath(import.meta.url)), "..")
const spec = JSON.parse(readFileSync(join(root, "openapi.json"), "utf8"))
const types = readFileSync(join(root, "src/client/types.gen.ts"), "utf8")
const camel = (id) =>
  id.replace(/[-_]+([a-zA-Z0-9])/g, (_, c) => c.toUpperCase())
const hasType = (name) => new RegExp(`export type ${name}\\b`).test(types)

function queryKind(schema = {}) {
  const variants = schema.anyOf ?? schema.oneOf ?? [schema]
  for (const variant of variants) {
    if (variant.type === "array") return "array"
    if (variant.type === "boolean") return "boolean"
    if (variant.type === "integer" || variant.type === "number") return "number"
  }
  return "string"
}

const entries = []
const imports = new Set()
for (const [path, ops] of Object.entries(spec.paths)) {
  for (const [method, op] of Object.entries(ops)) {
    const base = camel(op.operationId)
    const data = hasType(`${base}Data`) ? `${base}Data` : null
    const response = hasType(`${base}Response`) ? `${base}Response` : null
    if (data) imports.add(data)
    if (response) imports.add(response)
    // 查询参数在 URL 里都是字符串，记录 schema 类型供适配器还原成布尔/数字/数组。
    const query = {}
    for (const param of op.parameters ?? []) {
      if (param.in !== "query") continue
      const kind = queryKind(param.schema)
      if (kind !== "string") query[param.name] = kind
    }
    entries.push({
      id: op.operationId,
      method: method.toUpperCase(),
      path,
      data,
      response,
      query,
    })
  }
}
entries.sort((a, b) => a.id.localeCompare(b.id))

const out = `// 由 scripts/generate-demo-operations.mjs 生成，请勿手改。
import type {
${[...imports]
  .sort()
  .map((name) => `  ${name},`)
  .join("\n")}
} from "@/client/types.gen"

export const DEMO_OPERATIONS = {
${entries.map((e) => `  "${e.id}": { method: "${e.method}", path: "${e.path}", query: ${JSON.stringify(e.query)} },`).join("\n")}
} as const

export interface DemoOperationTypes {
${entries
  .map(
    (e) =>
      `  "${e.id}": { data: ${e.data ?? "unknown"}; response: ${e.response ?? "unknown"} }`,
  )
  .join("\n")}
}

export type DemoOperationId = keyof typeof DEMO_OPERATIONS
`
writeFileSync(join(root, "src/demo/operations.gen.ts"), out)
console.log(`demo operations: ${entries.length}`)
