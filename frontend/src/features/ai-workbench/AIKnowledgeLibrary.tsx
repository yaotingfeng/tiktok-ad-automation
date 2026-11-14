import { useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { aiRequest, errorMessage } from "./api"

type Knowledge = {
  id: string
  title: string
  scope: string
  version: number
  body: string
  source?: string
  source_verified?: boolean
  applicability?: string
  status?: string
  expires_at?: string | null
}
const scopeLabels: Record<string, string> = {
  PUBLIC: "公共知识",
  TENANT: "租户知识",
  BC: "BC 专属知识",
}
const statusLabels: Record<string, string> = {
  CONFIRMED: "已收录",
  CANDIDATE: "候选",
  RETIRED: "已停用",
}

/** 知识属于租户可见目录，不依赖投手；复盘实际引用以任务冻结版本为准。 */
export function AIKnowledgeLibrary({ tenantId }: { tenantId: string }) {
  const [items, setItems] = useState<Knowledge[]>([])
  const [selected, setSelected] = useState<Knowledge | null>(null)
  const [search, setSearch] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setItems([])
    setSelected(null)
    setSearch("")
    setLoading(true)
    setError(null)
    void aiRequest<Knowledge[]>(`/api/tenants/${tenantId}/ai-knowledge`, {
      signal: controller.signal,
    })
      .then((rows) => {
        if (!controller.signal.aborted) setItems(rows)
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [tenantId])
  const query = search.trim().toLocaleLowerCase()
  const visible = items.filter((item) =>
    `${item.title} ${item.body}`.toLocaleLowerCase().includes(query),
  )
  return (
    <Card data-testid="ai-knowledge-view">
      <CardHeader>
        <CardTitle>知识库</CardTitle>
        <CardDescription>
          浏览当前租户可见的政策、业务知识与经验。复盘实际使用内容以任务引用的版本为准。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Input
          aria-label="搜索知识"
          placeholder="搜索标题或正文"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        {loading ? (
          <p role="status">正在读取知识库…</p>
        ) : error ? (
          <p role="alert" className="text-sm text-destructive">
            知识库读取失败：{error}
          </p>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              已加载 {items.length} 项知识
              {query ? `，匹配 ${visible.length} 项` : ""}。
            </p>
            {!visible.length && (
              <p className="text-sm text-muted-foreground">
                {query ? "没有匹配的知识。" : "暂无知识。"}
              </p>
            )}
            <div className="flex flex-col gap-3">
              {visible.map((item) => (
                <div
                  key={item.id}
                  className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-md border p-4"
                >
                  <div className="flex min-w-0 flex-col gap-2">
                    <h3 className="break-words text-sm font-medium">
                      {item.title}
                    </h3>
                    <div className="flex flex-wrap gap-2">
                      <Badge variant="secondary">
                        {scopeLabels[item.scope] ?? "未标注范围"}
                      </Badge>
                      <Badge variant="outline">v{item.version}</Badge>
                      <Badge variant="outline">
                        {statusLabels[item.status ?? ""] ?? "未标注状态"}
                      </Badge>
                      {!item.source_verified && (
                        <Badge variant="outline">来源待核实</Badge>
                      )}
                    </div>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setSelected(item)}
                    aria-label={`查看详情：${item.title}`}
                  >
                    查看详情
                  </Button>
                </div>
              ))}
            </div>
          </>
        )}
        <Dialog
          open={!!selected}
          onOpenChange={(open) => {
            if (!open) setSelected(null)
          }}
        >
          <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>{selected?.title ?? "知识详情"}</DialogTitle>
              <DialogDescription>
                {selected
                  ? `${scopeLabels[selected.scope] ?? "未标注范围"} · v${selected.version} · ${selected.source_verified ? "来源已核实" : "来源待核实"}`
                  : ""}
              </DialogDescription>
            </DialogHeader>
            {selected && (
              <div className="flex min-w-0 flex-col gap-4 text-sm">
                <p className="whitespace-pre-wrap break-words">
                  {selected.body}
                </p>
                <dl className="flex flex-col gap-2 border-t pt-4 text-muted-foreground">
                  <dt className="font-medium">来源</dt>
                  <dd className="break-words">{selected.source || "未提供"}</dd>
                  <dt className="font-medium">适用条件</dt>
                  <dd className="whitespace-pre-wrap break-words">
                    {selected.applicability || "未提供，使用前需核对"}
                  </dd>
                  <dt className="font-medium">有效期</dt>
                  <dd>
                    {selected.expires_at
                      ? new Date(selected.expires_at).toLocaleString("zh-CN")
                      : "未标注到期时间，使用前需核对政策时效"}
                  </dd>
                </dl>
              </div>
            )}
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
