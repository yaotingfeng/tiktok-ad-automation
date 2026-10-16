import { useQuery } from "@tanstack/react-query"
import { ChevronRight } from "lucide-react"
import type { TemplatePublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
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
      <DialogContent className="flex max-h-[min(36rem,85dvh)] flex-col overflow-hidden sm:max-w-lg">
        <DialogHeader className="shrink-0 pr-6">
          <DialogTitle>从模板新建</DialogTitle>
          <DialogDescription>
            选择场景后调整条件。所有模板默认仅通知。
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 overflow-y-auto -mx-1 px-1">
          {templates.isPending && <Skeleton className="h-40 w-full" />}
          {templates.isError && (
            <div className="flex items-center justify-between gap-2 text-sm">
              <p>模板加载失败，请重试。</p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void templates.refetch()}
              >
                重试
              </Button>
            </div>
          )}
          {(templates.data?.items ?? []).map((template) => (
            <Button
              key={template.key}
              type="button"
              variant="ghost"
              className="h-auto w-full justify-between gap-3 whitespace-normal px-3 py-2.5 text-left"
              onClick={() => onPick(template)}
            >
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="font-medium">{template.name}</span>
                <span className="text-xs font-normal text-muted-foreground">
                  {template.description}
                </span>
              </span>
              <ChevronRight className="shrink-0" />
            </Button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
