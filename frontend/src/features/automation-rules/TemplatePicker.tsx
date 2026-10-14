import { useQuery } from "@tanstack/react-query"
import type { TemplatePublic } from "@/client"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { templatesQuery } from "./queries"

/** 从模板新建：选中后以模板名与定义打开编辑器，阈值由用户修改。 */
export function TemplatePicker({
  tenantId,
  onPick,
  onClose,
}: {
  tenantId: string
  onPick: (template: TemplatePublic) => void
  onClose: () => void
}) {
  const templates = useQuery(templatesQuery(tenantId))
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>从模板新建</DialogTitle>
          <DialogDescription>
            模板阈值为示例值，默认仅通知；请按实际情况修改后再选择执行模式。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-2 sm:grid-cols-2">
          {templates.isPending && (
            <p className="text-sm text-muted-foreground">加载中…</p>
          )}
          {(templates.data?.items ?? []).map((template) => (
            <button
              key={template.key}
              type="button"
              className="flex flex-col gap-1 rounded-md border p-3 text-left hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
              onClick={() => onPick(template)}
            >
              <span className="font-medium">{template.name}</span>
              <span className="text-sm text-muted-foreground">
                {template.description}
              </span>
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
