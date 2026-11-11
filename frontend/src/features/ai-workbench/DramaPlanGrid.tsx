import { useEffect, useMemo, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"

export type DramaPlanRow = {
  business_date: string
  scheduled_time: string
  title: string
  provider_connection_id: string
  application_id: string
  promotion_url: string
  external_drama_id: string
  order_materials: boolean
  optimizer_id: string
}

export const planColumns: Array<{ key: keyof DramaPlanRow; label: string }> = [
  { key: "scheduled_time", label: "时间" },
  { key: "title", label: "剧目名称" },
  { key: "provider_connection_id", label: "版权方连接" },
  { key: "application_id", label: "应用 ID" },
  { key: "promotion_url", label: "推广链接（可选）" },
  { key: "external_drama_id", label: "剧目 ID（文本）" },
  { key: "order_materials", label: "下单素材" },
]

export function emptyPlanRow(date: string, optimizerId: string): DramaPlanRow {
  return {
    business_date: date,
    scheduled_time: "",
    title: "",
    provider_connection_id: "",
    application_id: "",
    promotion_url: "",
    external_drama_id: "",
    order_materials: false,
    optimizer_id: optimizerId,
  }
}

function parseTsv(value: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let cell = ""
  let quoted = false
  const source = value.replace(/\r\n?/g, "\n")
  for (let i = 0; i < source.length; i += 1) {
    const char = source[i]
    if (char === '"' && (quoted || !cell)) {
      if (quoted && source[i + 1] === '"') {
        cell += '"'
        i += 1
      } else quoted = !quoted
    } else if (!quoted && (char === "\t" || char === "\n")) {
      row.push(cell)
      cell = ""
      if (char === "\n") {
        rows.push(row)
        row = []
      }
    } else cell += char
  }
  if (quoted) throw new Error("粘贴内容的引号未闭合")
  if (cell || row.length || !source.endsWith("\n")) rows.push([...row, cell])
  return rows
}

export function applyPlanPaste(
  rows: DramaPlanRow[],
  startRow: number,
  startColumn: number,
  value: string,
  date: string,
  optimizerId: string,
): DramaPlanRow[] {
  const incoming = parseTsv(value)
  if (incoming.some((line) => line.length + startColumn > planColumns.length))
    throw new Error("粘贴区域超出计划列")
  const result = rows.map((row) => ({ ...row }))
  while (result.length < startRow + incoming.length)
    result.push(emptyPlanRow(date, optimizerId))
  incoming.forEach((line, rowIndex) => {
    line.forEach((part, columnIndex) => {
      const key = planColumns[startColumn + columnIndex].key
      if (key === "order_materials")
        result[startRow + rowIndex][key] = /^(1|true|yes|是)$/i.test(part.trim())
      else result[startRow + rowIndex][key] = part
    })
  })
  return result
}

type Props = {
  businessDate: string
  optimizerId: string
  rows?: DramaPlanRow[]
  onChange?: (rows: DramaPlanRow[]) => void
  onDateChange?: (date: string) => void
  onSave?: (rows: DramaPlanRow[]) => Promise<void> | void
}

export function DramaPlanGrid({
  businessDate,
  optimizerId,
  rows: initialRows,
  onChange,
  onDateChange,
  onSave,
}: Props) {
  const initial = useMemo(
    () =>
      initialRows?.length
        ? initialRows
        : Array.from({ length: 10 }, () => emptyPlanRow(businessDate, optimizerId)),
    [businessDate, initialRows, optimizerId],
  )
  const [rows, setRows] = useState(initial)
  const [history, setHistory] = useState<DramaPlanRow[][]>([])
  const [saving, setSaving] = useState(false)
  const refs = useRef<Record<string, HTMLInputElement | null>>({})
  useEffect(() => {
    // Changing the business day loads a fresh ten-row worksheet and clears
    // undo history so rows from another day cannot be submitted accidentally.
    setRows(initial)
    setHistory([])
  }, [initial])
  const update = (next: DramaPlanRow[]) => {
    setHistory((past) => [...past.slice(-19), rows])
    setRows(next)
    onChange?.(next)
  }
  const undo = () => {
    const previous = history[history.length - 1]
    if (!previous) return
    setHistory((past) => past.slice(0, -1))
    setRows(previous)
    onChange?.(previous)
  }
  return (
    <Card data-testid="drama-plan-grid">
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>每日剧单</CardTitle>
        <div className="flex items-center gap-2">
          <label className="text-sm text-muted-foreground" htmlFor="plan-date">日期</label>
          <Input
            id="plan-date"
            data-testid="plan-date"
            type="date"
            value={businessDate}
            onChange={(event) => onDateChange?.(event.target.value)}
          />
          <Button type="button" variant="outline" onClick={undo} disabled={!history.length}>撤销</Button>
          {onSave && <Button type="button" onClick={async () => {
            setSaving(true)
            try { await onSave(rows) } finally { setSaving(false) }
          }} disabled={saving}>{saving ? "保存中…" : "保存剧单"}</Button>}
        </div>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="drama-plan-table">
          <thead><tr><th className="p-2 text-left">#</th>{planColumns.map((column) => <th className="p-2 text-left" key={column.key}>{column.label}</th>)}</tr></thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={`${rowIndex}-${row.title}`}>
                <td className="p-2 text-muted-foreground">{rowIndex + 1}</td>
                {planColumns.map((column, columnIndex) => {
                  const key = column.key
                  const refKey = `${rowIndex}:${columnIndex}`
                  return <td className="p-1" key={key}>
                    <Input
                      ref={(element) => { refs.current[refKey] = element }}
                      data-testid={`plan-cell-${rowIndex}-${columnIndex}`}
                      value={key === "order_materials" ? (row[key] ? "是" : "否") : row[key]}
                      onChange={(event) => {
                        const next = rows.map((item) => ({ ...item }))
                        next[rowIndex] = {
                          ...next[rowIndex],
                          [key]: key === "order_materials"
                            ? /^(是|1|true)$/i.test(event.target.value)
                            : event.target.value,
                        }
                        setRows(next)
                        onChange?.(next)
                      }}
                      onPaste={(event) => {
                        const text = event.clipboardData.getData("text/plain")
                        if (!text.includes("\t") && !text.includes("\n")) return
                        event.preventDefault()
                        update(applyPlanPaste(rows, rowIndex, columnIndex, text, businessDate, optimizerId))
                      }}
                      onKeyDown={(event) => {
                        if (event.key !== "Tab" && event.key !== "Enter") return
                        event.preventDefault()
                        const nextColumn = event.key === "Enter" ? columnIndex : columnIndex + 1
                        const nextRow = event.key === "Enter" ? rowIndex + 1 : rowIndex
                        refs.current[`${nextRow}:${nextColumn}`]?.focus()
                      }}
                    />
                  </td>
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-3 text-xs text-muted-foreground">素材只在执行时按剧目和版权方连接匹配；此处不选择素材。</p>
      </CardContent>
    </Card>
  )
}

