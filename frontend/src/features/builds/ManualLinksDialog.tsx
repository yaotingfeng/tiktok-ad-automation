import { useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import { type NamedManualLink, parseManualPaste } from "./manualLinks"

export function ManualLinksDialog({
  links,
  onSave,
  onClose,
}: {
  links: NamedManualLink[]
  onSave: (links: NamedManualLink[]) => void
  onClose: () => void
}) {
  const [text, setText] = useState(
    links
      .map((row) =>
        [
          row.title,
          row.url,
          row.external_drama_id || "",
          row.protected_base || "",
        ].join("\t"),
      )
      .join("\n"),
  )
  const [error, setError] = useState("")
  function save() {
    try {
      onSave(parseManualPaste(text))
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : "请检查粘贴内容")
    }
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>批量添加推广链接</DialogTitle>
          <DialogDescription>
            从 Excel
            同时复制「剧名、推广链接」两列，不含表头。同名剧补充链接，新剧追加到输入末尾。
          </DialogDescription>
        </DialogHeader>
        <Field data-invalid={!!error}>
          <FieldLabel htmlFor="manual-paste">剧名与推广链接</FieldLabel>
          <Textarea
            id="manual-paste"
            className="min-h-52 font-mono"
            value={text}
            onChange={(e) => {
              setText(e.target.value)
              setError("")
            }}
            aria-invalid={!!error}
            aria-describedby={error ? "manual-paste-error" : undefined}
            placeholder={
              "剧目 A\thttps://www.tiktok.com/minis/…\n剧目 B\thttps://www.tiktok.com/minis/…"
            }
          />
          {error && (
            <p
              id="manual-paste-error"
              role="alert"
              className="text-sm text-destructive"
            >
              {error}
            </p>
          )}
        </Field>
        <p className="text-sm text-muted-foreground">
          有补充信息时，第 3 列可填剧目 ID，第 4
          列可填归因名称。网眼需同时提供归因名称。也可先只输入剧名，在第二步补充链接和归因名称。
        </p>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button onClick={save}>添加到剧目</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
