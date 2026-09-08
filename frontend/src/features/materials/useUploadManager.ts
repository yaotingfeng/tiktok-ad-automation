import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useRef, useState } from "react"
import {
  MaterialsService,
  type UploadBatchResult,
  type UploadFileResult,
} from "@/client"
import { isForbidden } from "@/features/tenants/shared"
import { handleApiError } from "@/lib/api-feedback"
import { issue, isUnknown, materialKey } from "./presentation"
import {
  identity,
  type TransferProgress,
  type TransferRecord,
  transferFile,
} from "./upload-transfer"
export type PendingBatch = {
  requestId: string
  files: ReturnType<typeof identity>[]
}
type Saved = {
  records: Record<string, TransferRecord>
  pending?: PendingBatch
  batchIds: string[]
}
function restore(key: string): Saved {
  let saved: Saved = { records: {}, batchIds: [] }
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "null")
    if (
      value &&
      Array.isArray(value.batchIds) &&
      value.records &&
      typeof value.records === "object"
    )
      saved = value
  } catch {}
  try {
    const pending = JSON.parse(
      sessionStorage.getItem(`${key}:pending`) || "null",
    )
    saved.pending = pending?.requestId ? pending : undefined
  } catch {}
  return saved
}
export function useUploadManager(tenantId: string, bcId: string) {
  const [origin] = useState({ tenantId, bcId }),
    key = `materials-transfer:${origin.tenantId}:${origin.bcId}`
  const [saved] = useState(() => restore(key)),
    [pending, setPending] = useState(saved.pending),
    [creating, setCreating] = useState(false),
    [error, setError] = useState<unknown>(),
    [forbidden, setForbidden] = useState(false),
    [progress, setProgress] = useState<Record<string, TransferProgress>>({}),
    [batchIds, setBatchIds] = useState(saved.batchIds)
  const files = useRef(new Map<string, File>()),
    pendingFiles = useRef<File[]>([]),
    controllers = useRef(new Map<string, AbortController>()),
    creation = useRef<AbortController | null>(null),
    active = useRef(true),
    busy = useRef(false),
    denied = useRef(false),
    cache = useQueryClient()
  const persist = useCallback(() => {
    try {
      sessionStorage.setItem(key, JSON.stringify(saved))
    } catch {
      /* The current tab can continue; server progress remains recoverable. */
    }
  }, [key, saved])
  const revokePermission = useCallback(() => {
    if (denied.current) return
    denied.current = true
    setForbidden(true)
    for (const c of controllers.current.values()) c.abort()
    creation.current?.abort()
    setProgress((old) =>
      Object.fromEntries(
        Object.entries(old).map(([id, p]) => [
          id,
          p.busy
            ? {
                ...p,
                busy: false,
                error: true,
                message: "上传权限已失效，未完成的传输已暂停。",
              }
            : p,
        ]),
      ),
    )
  }, [])
  const fail = (e: unknown) => {
    if (!active.current) return
    setError(e)
    if (isForbidden(e)) revokePermission()
    if (e instanceof Error) handleApiError(e)
  }
  useEffect(() => {
    active.current = true
    return () => {
      active.current = false
      creation.current?.abort()
      for (const c of controllers.current.values()) c.abort()
    }
  }, [])
  const publish = (id: string, value: TransferProgress) => {
    if (active.current) setProgress((old) => ({ ...old, [id]: value }))
  }
  const refreshDirectory = () =>
    void cache.invalidateQueries({
      queryKey: materialKey(origin.tenantId, origin.bcId),
    })
  const register = (batch: UploadBatchResult, selected: File[] = []) => {
    if (batch.bc_id !== origin.bcId) throw new Error("scope_mismatch")
    if (!saved.batchIds.includes(batch.batch_id)) {
      saved.batchIds.unshift(batch.batch_id)
      setBatchIds([...saved.batchIds])
    }
    for (const row of batch.files) {
      const file = selected.find(
        (f) => f.name === row.file_name && f.size === row.byte_size,
      )
      if (file) {
        files.current.set(row.material_id, file)
        if (saved.records[row.material_id]?.uploadId !== row.upload_id)
          saved.records[row.material_id] = {
            identity: identity(file),
            uploadId: row.upload_id,
            parts: [],
          }
      }
    }
    saved.pending = undefined
    sessionStorage.removeItem(`${key}:pending`)
    setPending(undefined)
    persist()
    cache.setQueryData(
      [...materialKey(origin.tenantId, origin.bcId), "batch", batch.batch_id],
      batch,
    )
  }
  const run = async (batchId: string, row: UploadFileResult, file?: File) => {
    if (
      controllers.current.has(row.material_id) ||
      controllers.current.size >= 2 ||
      denied.current
    )
      return
    const local = file || files.current.get(row.material_id)
    if (!local) return
    if (local.name !== row.file_name || local.size !== row.byte_size) {
      publish(row.material_id, {
        bytes: 0,
        busy: false,
        error: true,
        message: "所选文件与原文件不一致，请重新选择原文件。",
      })
      return
    }
    files.current.set(row.material_id, local)
    let record = saved.records[row.material_id]
    if (!record || record.uploadId !== row.upload_id) {
      record = { identity: identity(local), uploadId: row.upload_id, parts: [] }
      saved.records[row.material_id] = record
      persist()
    }
    const controller = new AbortController()
    controllers.current.set(row.material_id, controller)
    try {
      await transferFile({
        tenantId: origin.tenantId,
        row,
        file: local,
        record,
        signal: controller.signal,
        changed: persist,
        progress: (value) => publish(row.material_id, value),
      })
    } catch (e) {
      if (controller.signal.aborted) return
      if (!isUnknown(e)) {
        record.completionUnknown = false
        persist()
      }
      const msg = e instanceof Error ? e.message : ""
      publish(row.material_id, {
        bytes: record.parts.reduce(
          (sum, p) =>
            sum +
            Math.min(
              row.part_size,
              row.byte_size - (p.part_number - 1) * row.part_size,
            ),
          0,
        ),
        busy: false,
        error: true,
        message:
          msg === "wrong_file"
            ? "所选文件与原文件不一致，请重新选择原文件。"
            : msg === "missing_etag"
              ? "存储未返回可读取的 ETag，分片未确认；请检查存储 CORS 配置。"
              : record.completionUnknown
                ? "原文件接收结果尚未确认，请刷新状态，不要重新创建批次。"
                : issue(e),
      })
      if (isForbidden(e)) {
        setForbidden(true)
        fail(e)
      }
    } finally {
      controllers.current.delete(row.material_id)
      if (active.current) {
        void cache.invalidateQueries({
          queryKey: [
            ...materialKey(origin.tenantId, origin.bcId),
            "batch",
            batchId,
          ],
        })
        refreshDirectory()
      }
    }
  }
  const runBatch = async (batch: UploadBatchResult) => {
    const rows = batch.files.filter(
      (r) => r.status === "receiving" && files.current.has(r.material_id),
    )
    let next = 0
    await Promise.all(
      Array.from({ length: Math.min(2, rows.length) }, async () => {
        while (next < rows.length && active.current) {
          const row = rows[next++]
          await run(batch.batch_id, row)
        }
      }),
    )
  }
  const start = async (
    selected: File[],
  ): Promise<UploadBatchResult | undefined> => {
    if (busy.current || saved.pending || denied.current) return
    busy.current = true
    setCreating(true)
    setError(undefined)
    const value = {
      requestId: crypto.randomUUID(),
      files: selected.map(identity),
    }
    try {
      sessionStorage.setItem(`${key}:pending`, JSON.stringify(value))
    } catch {
      busy.current = false
      setCreating(false)
      setError(new Error("resume_storage_unavailable"))
      return
    }
    saved.pending = value
    setPending(value)
    persist()
    pendingFiles.current = selected
    creation.current = new AbortController()
    try {
      const { data } = await MaterialsService.postUploadBatch({
        path: { tenant_id: origin.tenantId },
        body: {
          bc_id: origin.bcId,
          request_id: value.requestId,
          files: selected.map((f) => ({
            file_name: f.name,
            size: f.size,
            mime_type: f.type,
          })),
        },
        signal: creation.current.signal,
      })
      if (!active.current) return
      register(data, selected)
      void runBatch(data)
      refreshDirectory()
      return data
    } catch (e) {
      if (!active.current) return
      if (!isUnknown(e)) {
        saved.pending = undefined
        sessionStorage.removeItem(`${key}:pending`)
        setPending(undefined)
        persist()
      }
      fail(e)
    } finally {
      busy.current = false
      if (active.current) setCreating(false)
    }
  }
  const retry = async (batchId: string, row: UploadFileResult) => {
    if (
      !row.can_retry ||
      row.status !== "blocked" ||
      denied.current ||
      controllers.current.has(row.material_id)
    )
      return
    const controller = new AbortController()
    controllers.current.set(row.material_id, controller)
    publish(row.material_id, {
      bytes: row.received_bytes || 0,
      busy: true,
      message: "正在恢复明确失败的步骤",
    })
    try {
      const { data } = await MaterialsService.postObjectRetry({
        path: { tenant_id: origin.tenantId, material_id: row.material_id },
        signal: controller.signal,
      })
      controllers.current.delete(row.material_id)
      if (!active.current) return
      publish(row.material_id, { bytes: data.received_bytes || 0, busy: false })
      if (data.status === "receiving") {
        delete saved.records[row.material_id]
        persist()
      }
      if (data.status === "receiving") await run(batchId, data)
    } catch (e) {
      if (!controller.signal.aborted) {
        fail(e)
        publish(row.material_id, {
          bytes: row.received_bytes || 0,
          busy: false,
          error: true,
          message: issue(e),
        })
      }
    } finally {
      controllers.current.delete(row.material_id)
      refreshDirectory()
    }
  }
  const recover = async (): Promise<UploadBatchResult | undefined> => {
    if (!saved.pending || busy.current) return
    busy.current = true
    setCreating(true)
    creation.current = new AbortController()
    try {
      const { data } = await MaterialsService.readUploadRequest({
        path: {
          tenant_id: origin.tenantId,
          request_id: saved.pending.requestId,
        },
        query: { bc_id: origin.bcId },
        signal: creation.current.signal,
      })
      if (!active.current) return
      register(data, pendingFiles.current)
      void runBatch(data)
      setError(undefined)
      return data
    } catch (e) {
      if (active.current) fail(e)
    } finally {
      busy.current = false
      if (active.current) setCreating(false)
    }
  }
  const confirmCompletion = async (batch: UploadBatchResult) => {
    for (const row of batch.files) {
      const record = saved.records[row.material_id]
      if (
        row.status !== "receiving" ||
        !record?.completionUnknown ||
        record.parts.length !== row.part_count ||
        controllers.current.has(row.material_id)
      )
        continue
      const controller = new AbortController()
      controllers.current.set(row.material_id, controller)
      try {
        await MaterialsService.postComplete({
          path: { tenant_id: origin.tenantId, material_id: row.material_id },
          body: {
            parts: record.parts.map(({ part_number, etag }) => ({
              part_number,
              etag,
            })),
          },
          signal: controller.signal,
        })
        record.completionUnknown = false
        persist()
        publish(row.material_id, { bytes: row.byte_size, busy: false })
        refreshDirectory()
      } catch (e) {
        if (!controller.signal.aborted) fail(e)
      } finally {
        controllers.current.delete(row.material_id)
      }
    }
  }
  const observe = useCallback(
    (batch: UploadBatchResult) => {
      if (batch.bc_id !== origin.bcId) return
      let changed = false
      for (const row of batch.files) {
        if (
          row.received_bytes === row.byte_size &&
          row.status !== "receiving" &&
          row.status !== "result_unknown" &&
          saved.records[row.material_id]?.completionUnknown
        ) {
          saved.records[row.material_id].completionUnknown = false
          changed = true
          setProgress((old) => ({
            ...old,
            [row.material_id]: { bytes: row.byte_size, busy: false },
          }))
        }
      }
      if (changed) persist()
    },
    [origin.bcId, persist, saved],
  )
  return {
    revokePermission,
    recover,
    confirmCompletion,
    observe,
    start,
    register,
    runBatch,
    run,
    retry,
    pending,
    creating,
    error,
    forbidden,
    progress,
    batchIds,
    records: saved.records,
    hasFile: (id: string) => files.current.has(id),
    transferring: Object.values(progress).some((p) => p.busy),
    unfinished: Object.values(progress).some((p) => p.error),
    setError,
    origin,
  }
}
export type UploadManager = ReturnType<typeof useUploadManager>
