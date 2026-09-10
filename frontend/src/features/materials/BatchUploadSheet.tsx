import { useMemo, useState } from "react"
import type { IngestSummary } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { FieldGroup } from "@/components/ui/field"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { bytes, issue } from "./presentation"
import { fileIssue, UploadPanel } from "./UploadPanel"
import type { UploadManager } from "./useUploadManager"

const FILE_WINDOW = 100
const MAX_FILES = 20000

export function BatchUploadSheet({
  manager,
  onClose,
  onStarted,
}: {
  manager: UploadManager
  onClose: () => void
  onStarted: (batch: IngestSummary) => void
}) {
  // Index identity preserves independently selected files, including equal names/sizes.
  // Only the current window becomes rendered rows; File references never become bytes here.
  const [files, setFiles] = useState<ReadonlyMap<number, File>>(() => new Map())
  const [nextIndex, setNextIndex] = useState(0)
  const [page, setPage] = useState(0)
  const selection = useMemo(() => {
    const rows = [...files].map(([index, file]) => ({
      index,
      file,
      problem: fileIssue(file),
    }))
    return {
      rows,
      valid: rows.filter((row) => !row.problem).map((row) => row.file),
      totalBytes: rows.reduce((total, row) => total + row.file.size, 0),
    }
  }, [files])
  const currentPage = Math.min(
    page,
    Math.max(0, Math.ceil(files.size / FILE_WINDOW) - 1),
  )
  const visible = selection.rows.slice(
    currentPage * FILE_WINDOW,
    (currentPage + 1) * FILE_WINDOW,
  )
  const locked = manager.creating || !!manager.pending
  return (
    <ManagementSheet
      title="批量上传素材"
      description="系统自动安排合法上传账户。原文件仅临时中转，平台确认入库后自动清理。"
      dirty={files.size > 0 && !manager.pending}
      pending={manager.creating}
      onClose={onClose}
      actions={
        <Button
          disabled={
            !selection.valid.length ||
            files.size > MAX_FILES ||
            locked ||
            manager.forbidden
          }
          onClick={async () => {
            const batch = await manager.start(selection.valid)
            if (batch) onStarted(batch)
          }}
        >
          {manager.creating
            ? "正在建立导入会话…"
            : `开始上传 ${selection.valid.length} 个文件`}
        </Button>
      }
    >
      <FieldGroup>
        <UploadPanel
          disabled={locked || manager.forbidden}
          onFiles={(incoming) => {
            setFiles((previous) => {
              const next = new Map(previous)
              for (const [offset, file] of incoming.entries())
                next.set(nextIndex + offset, file)
              return next
            })
            setNextIndex((index) => index + incoming.length)
          }}
        />
        <p role="status" className="text-sm text-muted-foreground">
          已选择 {files.size} 个文件 · {bytes(selection.totalBytes)} ·{" "}
          {selection.valid.length} 个符合要求
        </p>
        {files.size > MAX_FILES && (
          <Alert variant="destructive">
            <AlertDescription>
              一次导入最多 20000 个文件，请移除部分文件后继续。
            </AlertDescription>
          </Alert>
        )}
        {!!manager.error && (
          <Alert variant="destructive">
            <AlertDescription>{issue(manager.error)}</AlertDescription>
          </Alert>
        )}
        {manager.pending && !manager.creating && (
          <Alert>
            <AlertDescription>
              导入创建结果尚未确认。关闭面板后可在上传队列按原请求核实；不会另建导入会话。
            </AlertDescription>
          </Alert>
        )}
        <ul className="flex flex-col gap-2">
          {visible.map(({ index, file, problem }) => (
            <li
              key={index}
              className="flex items-center justify-between gap-3 rounded-md border p-3"
            >
              <div className="min-w-0">
                <p className="break-all text-sm">{file.name}</p>
                <p className="text-xs text-muted-foreground">
                  {bytes(file.size)} · {problem || "可以上传"}
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                disabled={locked}
                aria-label={`移除待选文件 ${file.name}`}
                onClick={() =>
                  setFiles((previous) => {
                    const next = new Map(previous)
                    next.delete(index)
                    return next
                  })
                }
              >
                移除
              </Button>
            </li>
          ))}
        </ul>
        {files.size > FILE_WINDOW && (
          <nav
            aria-label="待选文件分页"
            className="flex items-center justify-between gap-2"
          >
            <Button
              variant="outline"
              disabled={currentPage === 0}
              aria-label="上一页待选文件"
              onClick={() => setPage(currentPage - 1)}
            >
              上一页
            </Button>
            <span className="text-sm text-muted-foreground">
              第 {currentPage + 1} / {Math.ceil(files.size / FILE_WINDOW)} 页 ·
              每页 100 个
            </span>
            <Button
              variant="outline"
              disabled={(currentPage + 1) * FILE_WINDOW >= files.size}
              aria-label="下一页待选文件"
              onClick={() => setPage(currentPage + 1)}
            >
              下一页
            </Button>
          </nav>
        )}
      </FieldGroup>
    </ManagementSheet>
  )
}
