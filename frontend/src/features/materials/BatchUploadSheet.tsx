import { useState } from "react"
import type { UploadBatchResult } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { FieldGroup } from "@/components/ui/field"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { bytes, issue } from "./presentation"
import { fileIssue, UploadPanel } from "./UploadPanel"
import type { UploadManager } from "./useUploadManager"
export function BatchUploadSheet({
  manager,
  onClose,
  onStarted,
}: {
  manager: UploadManager
  onClose: () => void
  onStarted: (batch: UploadBatchResult) => void
}) {
  const [files, setFiles] = useState<File[]>([]),
    [duplicate, setDuplicate] = useState(false)
  const selectionIssue = (file: File) =>
    fileIssue(file) ||
    (files.filter((f) => f.name === file.name && f.size === file.size).length >
    1
      ? "同名同大小文件请分批上传，以确保文件身份准确。"
      : null)
  const valid = files.filter((f) => !selectionIssue(f)),
    locked = manager.creating || !!manager.pending
  return (
    <ManagementSheet
      title="批量上传素材"
      description="系统自动安排上传账户。原文件完整接收后，平台上传由后台继续。"
      dirty={files.length > 0 && !manager.pending}
      pending={manager.creating}
      onClose={onClose}
      actions={
        <Button
          disabled={
            !valid.length || valid.length > 200 || locked || manager.forbidden
          }
          onClick={async () => {
            const batch = await manager.start(valid)
            if (batch) onStarted(batch)
          }}
        >
          {manager.creating
            ? "正在建立上传批次…"
            : `开始上传 ${valid.length} 个文件`}
        </Button>
      }
    >
      <FieldGroup>
        <UploadPanel
          disabled={locked || manager.forbidden}
          onFiles={(incoming) => {
            setDuplicate(false)
            setFiles((old) => {
              const next = [...old]
              for (const f of incoming) {
                if (
                  next.some(
                    (x) =>
                      x.name === f.name &&
                      x.size === f.size &&
                      x.lastModified === f.lastModified,
                  )
                )
                  setDuplicate(true)
                else next.push(f)
              }
              return next
            })
          }}
        />
        {duplicate && (
          <p role="status" className="text-sm text-muted-foreground">
            已跳过重复选择的文件。
          </p>
        )}
        {valid.length > 200 && (
          <Alert variant="destructive">
            <AlertDescription>
              单个批次最多 200 个文件，请移除部分文件后分批上传。
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
              批次创建结果尚未确认。关闭面板后可在上传队列按原请求核实；不会重复创建批次。
            </AlertDescription>
          </Alert>
        )}
        <ul className="flex flex-col gap-2">
          {files.map((file, i) => (
            <li
              key={`${file.name}-${file.size}-${i}`}
              className="flex items-center justify-between gap-3 rounded-md border p-3"
            >
              <div className="min-w-0">
                <p className="break-all text-sm">{file.name}</p>
                <p className="text-xs text-muted-foreground">
                  {bytes(file.size)} · {selectionIssue(file) || "可以上传"}
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                disabled={locked}
                aria-label={`移除待选文件 ${file.name}`}
                onClick={() =>
                  setFiles((old) => old.filter((_, index) => index !== i))
                }
              >
                移除
              </Button>
            </li>
          ))}
        </ul>
      </FieldGroup>
    </ManagementSheet>
  )
}
