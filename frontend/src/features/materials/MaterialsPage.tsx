import { useBlocker, useNavigate, useRouterState } from "@tanstack/react-router"
import { useCallback, useEffect, useRef, useState } from "react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { canManage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspaceEmpty } from "@/features/workspace/WorkspaceEmpty"
import { AssetDetails } from "./AssetDetails"
import { BatchHistory } from "./BatchHistory"
import { BatchUploadSheet } from "./BatchUploadSheet"
import { MaterialTable } from "./MaterialTable"
import { namingHint } from "./presentation"
import { UploadQueue } from "./UploadQueue"
import { useUploadManager } from "./useUploadManager"
export function MaterialsPage() {
  const { tenantId, scope, bc, bcPending } = useTenantScope()
  if (!tenantId || !scope?.bcId || !bc)
    return (
      <WorkspaceEmpty
        tenantId={tenantId}
        canConnect={canManage(scope?.role)}
        title="请先选择有效的 BC"
        description={
          bcPending
            ? "正在读取 BC 上下文。"
            : "素材上传与查询需要当前租户的 BC。请通过顶栏选择。"
        }
      />
    )
  return (
    <MaterialWorkspace
      key={`${tenantId}:${scope.bcId}`}
      tenantId={tenantId}
      bcId={scope.bcId}
      write={scope.role !== "viewer"}
    />
  )
}
function MaterialWorkspace({
  tenantId,
  bcId,
  write: allowed,
}: {
  tenantId: string
  bcId: string
  write: boolean
}) {
  const manager = useUploadManager(tenantId, bcId),
    [sheet, setSheet] = useState(false),
    [details, setDetails] = useState<string | null>(null),
    [started, setStarted] = useState<string | null>(null),
    navigate = useNavigate()
  const search = useRouterState({
      select: (s) => s.location.search as { tab?: string; batch_id?: string },
    }),
    tab = search.tab === "uploads" ? "uploads" : "library"
  useEffect(() => {
    if (!allowed) manager.revokePermission()
  }, [allowed, manager.revokePermission])
  const pendingFiles = useRef<HTMLInputElement>(null)
  const recoveryAttempted = useRef<string | null>(null)
  useEffect(() => {
    if (
      manager.pending &&
      !manager.creating &&
      recoveryAttempted.current !== manager.pending.requestId
    ) {
      recoveryAttempted.current = manager.pending.requestId
      void manager.recover().then((batch) => {
        if (batch) {
          setSheet(false)
          setStarted(batch.session_id)
        }
      })
    }
  }, [manager])
  const write = allowed && !manager.forbidden
  const blocker = useBlocker({
    shouldBlockFn: ({ next }) =>
      (manager.transferring || manager.unfinished || !!manager.pending) &&
      (next.pathname !== `/tenants/${tenantId}/materials` ||
        (next.search as { bc_id?: string }).bc_id !== bcId),
    enableBeforeUnload:
      manager.transferring || manager.unfinished || !!manager.pending,
    withResolver: true,
  })
  const go = useCallback(
    (next: string, batchId = search.batch_id) =>
      void navigate({
        to: "/tenants/$tenantId/materials",
        params: { tenantId },
        search: {
          bc_id: bcId,
          tab: next as "uploads" | "library",
          batch_id: batchId,
        },
      }),
    [navigate, tenantId, bcId, search.batch_id],
  )
  useEffect(() => {
    if (started) {
      go("uploads", started)
      setStarted(null)
    }
  }, [started, go])
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-2">
          <h1 className="workspace-title">素材库</h1>
          <p className="text-sm text-muted-foreground">
            视频临时中转，平台确认入库后清理原件；保留实际上传账户与可用素材记录。
          </p>
        </div>
        {write && (
          <Button
            onClick={() => setSheet(true)}
            disabled={!!manager.pending || manager.transferring}
          >
            批量上传
          </Button>
        )}
      </div>
      <Alert>
        <AlertDescription>{namingHint}</AlertDescription>
      </Alert>
      {manager.pending && !manager.creating && (
        <Alert>
          <AlertDescription>
            <p>导入创建结果尚未确认，按原请求核实。未查到结果不代表未创建。</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                void manager.recover().then((batch) => {
                  if (batch) {
                    setSheet(false)
                    setStarted(batch.session_id)
                  }
                })
              }
            >
              核实导入创建结果
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={manager.creating || !manager.pending.metadataReady}
              onClick={() =>
                void manager.retryCreation().then((session) => {
                  if (session) setStarted(session.session_id)
                })
              }
            >
              按原请求继续创建
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {manager.pending && !manager.pending.metadataReady && (
        <Alert>
          <AlertDescription>
            <p>
              本地文件登记曾中断。请重新选择此次导入的全部文件，按原请求继续。
            </p>
            <Button
              variant="outline"
              disabled={manager.creating}
              onClick={() => pendingFiles.current?.click()}
            >
              重选此导入全部文件
            </Button>
            <Input
              ref={pendingFiles}
              aria-label="重选此导入全部文件"
              className="hidden"
              type="file"
              multiple
              accept="video/*"
              onChange={(event) => {
                const files = Array.from(event.target.files ?? [])
                event.target.value = ""
                if (files.length)
                  void manager.restorePendingFiles(files).then((session) => {
                    if (session) setStarted(session.session_id)
                  })
              }}
            />
          </AlertDescription>
        </Alert>
      )}
      <Card>
        <CardContent className="p-0">
          <Tabs value={tab} onValueChange={(v) => go(v)}>
            <TabsList className="m-4">
              <TabsTrigger value="library">素材库</TabsTrigger>
              <TabsTrigger value="uploads">上传队列</TabsTrigger>
            </TabsList>
          </Tabs>
          <section aria-label="素材目录" hidden={tab !== "library"}>
            <MaterialTable
              tenantId={tenantId}
              bcId={bcId}
              onDetails={setDetails}
              onForbidden={manager.revokePermission}
              enabled={tab === "library"}
            />
          </section>
          <section aria-label="上传批次与队列" hidden={tab !== "uploads"}>
            {search.batch_id ? (
              <UploadQueue
                key={search.batch_id}
                tenantId={tenantId}
                bcId={bcId}
                batchId={search.batch_id}
                manager={manager}
                write={write}
                onDetails={setDetails}
                onForbidden={manager.revokePermission}
                onHistory={() =>
                  void navigate({
                    to: "/tenants/$tenantId/materials",
                    params: { tenantId },
                    search: { bc_id: bcId, tab: "uploads" },
                  })
                }
              />
            ) : (
              <BatchHistory
                enabled={tab === "uploads"}
                onForbidden={manager.revokePermission}
                tenantId={tenantId}
                bcId={bcId}
                onOpen={(id) => go("uploads", id)}
              />
            )}
          </section>
        </CardContent>
      </Card>
      {sheet && (
        <BatchUploadSheet
          manager={manager}
          onClose={() => setSheet(false)}
          onStarted={(batch) => {
            setSheet(false)
            setStarted(batch.session_id)
          }}
        />
      )}
      {details && (
        <AssetDetails
          key={details}
          tenantId={tenantId}
          bcId={bcId}
          materialId={details}
          onClose={() => setDetails(null)}
          onForbidden={manager.revokePermission}
        />
      )}
      <Dialog
        open={blocker.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blocker.reset?.()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>暂停本地传输并离开？</DialogTitle>
            <DialogDescription>
              未完整接收的文件将暂停，需要返回原租户与 BC
              继续传输。已接收文件的后台处理继续，已创建批次不会删除或改绑。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => blocker.reset?.()}>
              留在当前页
            </Button>
            <Button
              disabled={manager.creating}
              onClick={() => blocker.proceed?.()}
            >
              暂停并离开
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
