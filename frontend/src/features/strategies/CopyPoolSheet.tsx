import { useQuery } from "@tanstack/react-query"
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { StrategyError as RequestError } from "./feedback"
import { copyPoolQuery } from "./queries"
export function CopyPoolSheet({
  tenantId,
  versionId,
  onClose,
}: {
  tenantId: string
  versionId: string
  onClose: () => void
}) {
  const query = useQuery(copyPoolQuery(tenantId, versionId))
  const capacity = query.data
    ? new Set(
        query.data.entries.filter((r) => r.text.trim()).map((r) => r.text),
      ).size
    : undefined
  return (
    <ManagementSheet
      title="英文文案池"
      description="只读查看已发布的英文文案；实际抽样在搭建预览完成。"
      dirty={false}
      onClose={onClose}
    >
      {query.isPending && <Skeleton className="h-32" />}
      {query.error && (
        <RequestError error={query.error} retry={() => void query.refetch()} />
      )}{" "}
      {query.data && (
        <div className="flex flex-col gap-4">
          <h3 className="font-semibold">{query.data.name}</h3>
          <p className="break-all text-xs text-muted-foreground">
            版本 ID：{query.data.id}
          </p>
          <p>当前有效去重正文：{capacity} 条 · 英文</p>
          {query.data.entries.length ? (
            <ol className="flex flex-col gap-2 text-sm">
              {query.data.entries.map((row) => (
                <li key={row.id} className="rounded-md border p-3">
                  <span className="mr-2 text-muted-foreground">
                    {row.position}.
                  </span>
                  {row.text}
                </li>
              ))}
            </ol>
          ) : (
            <Empty>
              <EmptyHeader>
                <EmptyTitle>该版本暂无有效文案</EmptyTitle>
              </EmptyHeader>
            </Empty>
          )}
        </div>
      )}
    </ManagementSheet>
  )
}
