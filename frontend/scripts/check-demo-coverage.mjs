// 体验版接口覆盖检查：openapi.json 里的每个操作都必须在 src/demo/handlers 注册处理函数，
// 或在 src/demo/coverage-exemptions.ts 写明豁免原因。生产新增接口而体验版未跟上时在 CI 失败。
import { readdirSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const root = join(dirname(fileURLToPath(import.meta.url)), "..")
const spec = JSON.parse(readFileSync(join(root, "openapi.json"), "utf8"))
const operations = new Set(
  Object.values(spec.paths).flatMap((ops) =>
    Object.values(ops).map((op) => op.operationId),
  ),
)

const generated = readFileSync(join(root, "src/demo/operations.gen.ts"), "utf8")
const stale = [...operations].filter((id) => !generated.includes(`"${id}":`))
if (stale.length) {
  console.error(
    `src/demo/operations.gen.ts 已过期，请运行 node scripts/generate-demo-operations.mjs：\n  ${stale.join("\n  ")}`,
  )
  process.exit(1)
}

const handlerDir = join(root, "src/demo/handlers")
const files = readdirSync(handlerDir, { recursive: true })
  .filter((f) => String(f).endsWith(".ts") || String(f).endsWith(".tsx"))
  .map((f) => join(handlerDir, String(f)))
const registered = new Map()
for (const file of files) {
  for (const match of readFileSync(file, "utf8").matchAll(
    /\bhandle\(\s*["']([^"']+)["']/g,
  )) {
    if (registered.has(match[1])) {
      console.error(`重复注册：${match[1]}（${file}）`)
      process.exit(1)
    }
    registered.set(match[1], file)
  }
}

const exemptionsSource = readFileSync(
  join(root, "src/demo/coverage-exemptions.ts"),
  "utf8",
)
const exempt = new Map(
  [
    ...exemptionsSource.matchAll(
      /["']([a-z_-]+-[a-z_]+)["']\s*:\s*["']([^"']*)["']/g,
    ),
  ].map((m) => [m[1], m[2]]),
)
const problems = []
for (const [id, reason] of exempt)
  if (!reason.trim()) problems.push(`豁免缺少原因：${id}`)
for (const id of registered.keys())
  if (!operations.has(id)) problems.push(`处理函数对应的接口不存在：${id}`)
for (const id of exempt.keys())
  if (!operations.has(id)) problems.push(`豁免的接口不存在：${id}`)
const missing = [...operations].filter(
  (id) => !registered.has(id) && !exempt.has(id),
)
for (const id of missing.sort()) problems.push(`未覆盖：${id}`)

if (problems.length) {
  console.error(
    `体验版接口覆盖检查失败（${problems.length} 项）：\n  ${problems.join("\n  ")}`,
  )
  process.exit(1)
}
console.log(
  `体验版接口覆盖检查通过：${registered.size} 个已模拟，${exempt.size} 个豁免，共 ${operations.size} 个。`,
)
