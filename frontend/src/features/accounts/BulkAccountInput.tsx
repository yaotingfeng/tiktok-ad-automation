import { useId } from "react"
import type { ResolvedLine } from "@/client"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"

const labels: Record<ResolvedLine["status"], string> = {
  MATCHED: "已解析，可直接使用",
  DUPLICATE: "重复账户",
  AMBIGUOUS: "名称有歧义",
  NOT_FOUND: "未找到",
  EMPTY: "空行",
  BLOCKED: "当前不可用",
}
export type BulkAccountInputProps = {
  value: string
  onChange: (value: string) => void
  onResolve: () => void
  rows: ResolvedLine[]
  pending?: boolean
}
// Parent owns tenant/BC binding and paginates resolved results. Only current-page
// rows render here; matching never requires a second selection or mutates drafts.
export function BulkAccountInput({
  value,
  onChange,
  onResolve,
  rows,
  pending = false,
}: BulkAccountInputProps) {
  const id = useId()
  return (
    <section className="flex flex-col gap-4" aria-label="批量账户输入">
      <Field>
        <FieldLabel htmlFor={id}>批量粘贴账户</FieldLabel>
        <Textarea
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="每行一个账户 ID 或完整账户名称"
          rows={6}
        />
        <FieldDescription>
          成功解析的账户直接用于搭建，无需再次勾选。有歧义时请改为完整账户 ID。
        </FieldDescription>
      </Field>
      <Button
        type="button"
        disabled={pending || !value.trim()}
        onClick={onResolve}
      >
        {pending ? "正在解析…" : "解析账户"}
      </Button>
      <ul
        className="flex max-h-[60svh] flex-col gap-3 overflow-auto"
        aria-label="当前页解析结果"
      >
        {rows.map((row) => (
          <li key={row.line_no} className="rounded-md border p-3 text-sm">
            <p className="break-all">
              第 {row.line_no} 行：{row.raw || "（空行）"}
            </p>
            <p>
              {labels[row.status]}
              {row.reason ? `：${row.reason}` : ""}
            </p>
            {row.advertiser_id && (
              <p className="break-all font-mono text-xs">
                账户 ID：{row.advertiser_id}
              </p>
            )}
            {row.duplicate_of != null && <p>与第 {row.duplicate_of} 行重复</p>}
            {!!row.candidates?.length && (
              <p className="break-all">候选 ID：{row.candidates.join("、")}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
