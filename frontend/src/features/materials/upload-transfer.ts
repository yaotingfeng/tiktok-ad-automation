import {
  MaterialsService,
  type UploadedPart,
  type UploadFileResult,
} from "@/client"
export type FileIdentity = {
  name: string
  size: number
  type: string
  lastModified: number
}
export type TransferRecord = {
  identity: FileIdentity
  uploadId: string
  parts: Array<UploadedPart & { hash: string }>
  completionUnknown?: boolean
}
export type TransferProgress = {
  bytes: number
  busy: boolean
  message?: string
  error?: boolean
}
export const identity = (file: File): FileIdentity => ({
  name: file.name,
  size: file.size,
  type: file.type,
  lastModified: file.lastModified,
})
const digest = async (blob: Blob) =>
  Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()),
    ),
  )
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("")
export async function transferFile({
  tenantId,
  row,
  file,
  record,
  signal,
  changed,
  progress,
}: {
  tenantId: string
  row: UploadFileResult
  file: File
  record: TransferRecord
  signal: AbortSignal
  changed: () => void
  progress: (value: TransferProgress) => void
}) {
  if (
    file.name !== row.file_name ||
    file.size !== row.byte_size ||
    JSON.stringify(identity(file)) !== JSON.stringify(record.identity)
  )
    throw new Error("wrong_file")
  if (row.status !== "receiving") return
  if (
    !Number.isSafeInteger(row.part_size) ||
    row.part_size <= 0 ||
    row.part_count !== Math.ceil(file.size / row.part_size) ||
    row.part_count > 10000
  )
    throw new Error("invalid_session")
  const sizeOf = (part: number) =>
    Math.min(row.part_size, file.size - (part - 1) * row.part_size)
  let confirmed = record.parts.reduce((n, p) => n + sizeOf(p.part_number), 0)
  progress({
    bytes: confirmed,
    busy: true,
    message: record.parts.length
      ? "正在校验原文件与已确认分片"
      : "正在传输原文件",
  })
  // Re-selected files may reuse parts only after checking their bytes, not just names.
  for (const part of record.parts) {
    signal.throwIfAborted()
    if (
      (await digest(
        file.slice(
          (part.part_number - 1) * row.part_size,
          part.part_number * row.part_size,
        ),
      )) !== part.hash
    )
      throw new Error("wrong_file")
  }
  for (let n = 1; n <= row.part_count; n++) {
    signal.throwIfAborted()
    if (record.parts.some((p) => p.part_number === n)) continue
    const blob = file.slice((n - 1) * row.part_size, n * row.part_size),
      hash = await digest(blob)
    let etag: string | null = null
    for (let attempt = 0; attempt < 3; attempt++) {
      signal.throwIfAborted()
      const signed = await MaterialsService.postPartSignature({
        path: {
          tenant_id: tenantId,
          material_id: row.material_id,
          part_number: n,
        },
        signal,
      })
      try {
        // Never attach application Bearer credentials to a presigned object URL.
        const response = await fetch(signed.data.url, {
          method: "PUT",
          body: blob,
          signal,
          credentials: "omit",
          referrerPolicy: "no-referrer",
        })
        if (!response.ok) {
          if (
            response.status >= 500 ||
            response.status === 403 ||
            response.status === 408 ||
            response.status === 429
          )
            throw new Error("part_failed")
          throw new Error("part_rejected")
        }
        etag = response.headers.get("ETag")
        if (!etag) throw new Error("missing_etag")
        break
      } catch (e) {
        if (
          signal.aborted ||
          attempt === 2 ||
          (e instanceof Error &&
            ["part_rejected", "missing_etag"].includes(e.message))
        )
          throw e
      }
    }
    signal.throwIfAborted()
    if (!etag) throw new Error("missing_etag")
    record.parts.push({ part_number: n, etag, hash })
    confirmed += blob.size
    changed()
    progress({ bytes: confirmed, busy: true, message: "正在传输原文件" })
  }
  record.parts.sort((a, b) => a.part_number - b.part_number)
  record.completionUnknown = true
  changed()
  progress({ bytes: file.size, busy: true, message: "正在确认原文件接收" })
  await MaterialsService.postComplete({
    path: { tenant_id: tenantId, material_id: row.material_id },
    body: {
      parts: record.parts.map(({ part_number, etag }) => ({
        part_number,
        etag,
      })),
    },
    signal,
  })
  record.completionUnknown = false
  changed()
  progress({
    bytes: file.size,
    busy: false,
    message: "原文件接收已确认，平台进度由后台核实",
  })
}
