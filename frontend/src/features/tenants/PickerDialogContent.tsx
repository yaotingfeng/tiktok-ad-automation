import type { ReactNode } from "react"
import {
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

// 单选目录和账户多选共用紧凑弹窗外壳，各自维护选择行为。
export function PickerDialogContent({
  id,
  title,
  description,
  contained = false,
  children,
}: {
  id?: string
  title: string
  description: string
  contained?: boolean
  children: ReactNode
}) {
  return (
    <DialogContent
      id={id}
      className={cn(
        "max-h-[90svh]",
        contained ? "flex flex-col overflow-hidden" : "overflow-y-auto",
      )}
    >
      <DialogHeader className="shrink-0">
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      {children}
    </DialogContent>
  )
}
