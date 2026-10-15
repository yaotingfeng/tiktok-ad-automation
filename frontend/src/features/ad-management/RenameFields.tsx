import { Info } from "lucide-react"
import type { ReactNode } from "react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import {
  parsePastedRows,
  type RenameDraft,
  type RenameKind,
} from "./renameRules"

function Hint({ children }: { children: ReactNode }) {
  return (
    <Alert>
      <Info />
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  )
}

function TextField({
  id,
  label,
  value,
  placeholder,
  onChange,
}: {
  id: string
  label: string
  value: string
  placeholder: string
  onChange: (value: string) => void
}) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  )
}

export function RenameFields({
  value,
  onChange,
}: {
  value: RenameDraft
  onChange: (value: RenameDraft) => void
}) {
  const set = (patch: Partial<RenameDraft>) => onChange({ ...value, ...patch })
  const parsed = parsePastedRows(value.text)
  return (
    <Tabs
      value={value.kind}
      onValueChange={(kind) => set({ kind: kind as RenameKind })}
    >
      <TabsList>
        <TabsTrigger value="replace">查找替换</TabsTrigger>
        <TabsTrigger value="affix">前缀后缀</TabsTrigger>
        <TabsTrigger value="explicit">表格粘贴</TabsTrigger>
      </TabsList>
      <TabsContent value="replace" className="flex flex-col gap-3">
        <Hint>
          把名称中出现的文字全部替换成新文字，区分大小写；“替换为”留空即删除这段文字。名称中不含查找文字的推广系列会跳过。
          例如：查找 0901，替换为 1015。
        </Hint>
        <div className="grid grid-cols-2 gap-3">
          <TextField
            id="rename-find"
            label="查找"
            value={value.find}
            placeholder="例如 0901"
            onChange={(find) => set({ find })}
          />
          <TextField
            id="rename-replace-with"
            label="替换为"
            value={value.replaceWith}
            placeholder="例如 1015"
            onChange={(replaceWith) => set({ replaceWith })}
          />
        </div>
      </TabsContent>
      <TabsContent value="affix" className="flex flex-col gap-3">
        <Hint>
          在名称最前面或最后面加上文字，至少填写一项。名称已以该前缀开头或以该后缀结尾的会跳过，不会重复添加。
          例如：前缀填【停】，名称变为“【停】原名称”。
        </Hint>
        <div className="grid grid-cols-2 gap-3">
          <TextField
            id="rename-prefix"
            label="前缀"
            value={value.prefix}
            placeholder="例如 【停】"
            onChange={(prefix) => set({ prefix })}
          />
          <TextField
            id="rename-suffix"
            label="后缀"
            value={value.suffix}
            placeholder="例如 -1015"
            onChange={(suffix) => set({ suffix })}
          />
        </div>
      </TabsContent>
      <TabsContent value="explicit" className="flex flex-col gap-3">
        <Hint>
          粘贴两列：推广系列 ID、新名称，一行一条，用 Tab
          或逗号分隔；首行不是数字 ID
          时视为表头忽略。可直接从飞书表格或导出的报表复制，单次最多 500 行。
        </Hint>
        <Textarea
          aria-label="粘贴推广系列 ID 和新名称"
          className="min-h-32 font-mono text-xs"
          placeholder={
            "推广系列ID\t新名称\n1700000000000000001\t嘉书-剧名-1015"
          }
          value={value.text}
          onChange={(event) => set({ text: event.target.value })}
        />
        <div className="text-xs text-muted-foreground">
          已识别 {parsed.rows.length} 行
          {parsed.errors.length > 0 && (
            <ul className="mt-1 list-disc pl-4 text-destructive">
              {parsed.errors.map((error) => (
                <li key={error}>{error}</li>
              ))}
            </ul>
          )}
        </div>
      </TabsContent>
    </Tabs>
  )
}
