import { useRef } from "react"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { namingHint } from "./presentation"
import { MAX_BROWSER_FILE_BYTES } from "./upload-scheduler"
export const supportedTypes = new Set([
  "video/mp4",
  "video/quicktime",
  "video/x-msvideo",
  "video/webm",
  "video/mpeg",
  "video/x-matroska",
])
export function fileIssue(file: File) {
  if (!file.size) return "文件为空"
  if (file.size > MAX_BROWSER_FILE_BYTES)
    return "文件超过单文件 1 GiB（1024 MiB）上限"
  if (!supportedTypes.has(file.type))
    return "请选择 MP4、MOV、AVI、WebM、MPEG 或 MKV 视频"
  if (
    !file.name.trim() ||
    [...file.name].length > 1000 ||
    [...file.name].some((char) => char.charCodeAt(0) < 32)
  )
    return "文件名无效"
  return null
}
export function UploadPanel({
  onFiles,
  disabled = false,
}: {
  onFiles: (files: File[]) => void
  disabled?: boolean
}) {
  const input = useRef<HTMLInputElement>(null)
  return (
    <Field data-disabled={disabled}>
      <FieldLabel htmlFor="material-files">选择本地素材</FieldLabel>
      <FieldDescription>{namingHint}</FieldDescription>
      <section
        aria-label="本地视频拖放区"
        className="flex min-w-0 flex-col items-start gap-3 rounded-lg border border-dashed p-4 lg:p-6"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault()
          if (!disabled) onFiles(Array.from(e.dataTransfer.files))
        }}
      >
        <p className="text-sm text-muted-foreground">
          一次最多选择 20000 个视频，每个文件最多 1 GiB（1024
          MiB）。刷新后未传完的文件需要重新选择。
        </p>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          onClick={() => input.current?.click()}
        >
          选择本地素材
        </Button>
        <Input
          ref={input}
          className="hidden"
          tabIndex={-1}
          id="material-files"
          type="file"
          accept="video/*"
          multiple
          disabled={disabled}
          onChange={(e) => {
            onFiles(Array.from(e.target.files || []))
            e.target.value = ""
          }}
        />
      </section>
    </Field>
  )
}
