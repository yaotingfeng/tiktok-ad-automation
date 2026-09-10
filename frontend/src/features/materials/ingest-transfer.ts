/** Generated HTTP contracts adapted to the browser's private scheduling interface. */
import { AxiosError } from "axios"
import {
  type IngestFilePublic,
  type IngestIdentity,
  MaterialIngestService,
} from "@/client"
import { handleApiError } from "@/lib/api-feedback"
import type { UploadPermissionLedger } from "./upload-permissions"
import type { PermissionCallbacks, TransferCallbacks } from "./upload-scheduler"
import {
  type MultipartIdentity,
  UploadError,
  type UploadFileRecord,
} from "./upload-store"

export const canRestartOriginal = (file: IngestFilePublic) =>
  file.can_retry &&
  file.operation_status !== "result_unknown" &&
  ["deleted", "waiting_capacity"].includes(file.temporary_storage_status) &&
  ["blocked", "failed"].includes(file.platform_status)

export const receivedOriginal = (file: IngestFilePublic) =>
  file.received_bytes === file.size &&
  [
    "stored",
    "validating",
    "verified",
    "cleanup_pending",
    "deleting",
    "delete_unknown",
    "deleted",
  ].includes(file.temporary_storage_status)

export function ingestIdentity(
  file: Pick<
    IngestFilePublic,
    "generation" | "upload_id" | "operation_revision"
  >,
): IngestIdentity {
  return {
    generation: file.generation,
    upload_id: file.upload_id,
    operation_revision: file.operation_revision,
  }
}
export function multipart(file: IngestFilePublic): MultipartIdentity {
  if (!file.upload_id) throw new UploadError("response_invalid")
  return {
    materialId: file.material_id,
    generation: file.generation,
    uploadId: file.upload_id,
    operationRevision: file.operation_revision,
    partSize: file.part_size,
    partCount: file.part_count,
  }
}
function requestIdentity(identity: MultipartIdentity): IngestIdentity {
  return {
    generation: identity.generation,
    upload_id: identity.uploadId,
    operation_revision: identity.operationRevision,
  }
}
function checkFile(file: IngestFilePublic, record: UploadFileRecord) {
  if (
    file.material_id !== record.materialId ||
    file.client_index !== record.clientIndex ||
    file.file_name !== record.file.name ||
    file.size !== record.file.size ||
    file.mime_type !== record.file.type ||
    file.last_modified_ms !== record.file.lastModified
  )
    throw new UploadError("response_invalid")
}
export function transferError(error: unknown): Error {
  if (
    error instanceof UploadError ||
    (error instanceof Error && error.name === "AbortError")
  )
    return error
  if (error instanceof AxiosError) {
    if (error.response?.status === 401 || error.response?.status === 403)
      return new UploadError("permission_denied")
    const code = error.response?.data?.code
    if (
      [
        "storage_backpressure",
        "ingest_capacity_exceeded",
        "capacity_exhausted",
        "temporary_storage_full",
      ].includes(code)
    )
      return new UploadError("storage_backpressure", 5000)
    if (
      [
        "upload_identity_changed",
        "object_generation_conflict",
        "stale_object_revision",
      ].includes(code)
    )
      return new UploadError("upload_identity_changed")
  }
  return new UploadError("transfer_unavailable")
}
export function delay(ms: number, signal: AbortSignal) {
  signal.throwIfAborted()
  return new Promise<void>((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer)
      reject(signal.reason)
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort)
      resolve()
    }, ms)
    signal.addEventListener("abort", abort, { once: true })
  })
}
export function ingestCallbacks(options: {
  tenantId: string
  sessionId: () => string
  guard: (signal: AbortSignal) => void
  observe: (file: IngestFilePublic) => void
  ledger: UploadPermissionLedger
}): TransferCallbacks & { permissions: PermissionCallbacks } {
  const path = (materialId: string) => ({
    tenant_id: options.tenantId,
    session_id: options.sessionId(),
    material_id: materialId,
  })
  const invoke = async <T>(
    signal: AbortSignal,
    work: () => Promise<{ data: T }>,
  ) => {
    options.guard(signal)
    try {
      const result = await work()
      options.guard(signal)
      return result.data
    } catch (error) {
      if (error instanceof Error) handleApiError(error)
      options.guard(signal)
      throw transferError(error)
    }
  }
  const observe = (file: IngestFilePublic, record: UploadFileRecord) => {
    checkFile(file, record)
    options.observe(file)
    return file
  }
  const checkIdentity = (
    result: {
      generation: number
      upload_id: string | null
      operation_revision: number
    },
    identity: MultipartIdentity,
  ) => {
    if (
      result.generation !== identity.generation ||
      result.upload_id !== identity.uploadId ||
      result.operation_revision < identity.operationRevision
    )
      throw new UploadError("upload_identity_changed")
  }
  return {
    permissions: {
      ledger: options.ledger,
      async readSignPermission(input, signal) {
        const result = await invoke(signal, () =>
          MaterialIngestService.readIngestPartPermissions({
            path: path(input.identity.materialId),
            query: {
              ...requestIdentity(input.identity),
              upload_id: input.identity.uploadId,
              request_id: input.requestId,
            },
            signal,
          }),
        )
        checkIdentity(result, input.identity)
        if (result.request_id !== input.requestId || result.items.length > 1)
          throw new UploadError("response_invalid")
        const part = result.items[0]
        if (!part) return null
        if (part.part_number !== input.partNumber)
          throw new UploadError("response_invalid")
        return {
          permission: {
            id: part.permission_id,
            nonce: part.permission_nonce,
            revision: part.permission_revision,
            partNumber: part.part_number,
          },
          outcome: part.outcome,
        }
      },
      async acknowledgeReceipts(input, signal) {
        const result = await invoke(signal, () =>
          MaterialIngestService.acknowledgeIngestParts({
            path: path(input.identity.materialId),
            body: {
              ...requestIdentity(input.identity),
              upload_id: input.identity.uploadId,
              receipts: input.receipts.map(({ permission, outcome, etag }) => ({
                part_number: permission.partNumber,
                permission_id: permission.id,
                permission_nonce: permission.nonce,
                permission_revision: permission.revision,
                outcome,
                etag,
              })),
            },
            signal,
          }),
        )
        checkIdentity(result, input.identity)
        return result.accepted_permission_ids
      },
    },
    async registerChunk(input, signal) {
      const result = await invoke(signal, () =>
        MaterialIngestService.createIngestChunk({
          path: {
            tenant_id: options.tenantId,
            session_id: options.sessionId(),
          },
          body: {
            request_id: input.requestId,
            files: input.files.map(({ clientIndex, file }) => ({
              client_index: clientIndex,
              file_name: file.name,
              size: file.size,
              mime_type: file.type,
              last_modified_ms: file.lastModified,
            })),
          },
          signal,
        }),
      )
      if (
        result.session_id !== options.sessionId() ||
        result.request_id !== input.requestId
      )
        throw new UploadError("response_invalid")
      return result.items.map((file) => ({
        clientIndex: file.client_index,
        materialId: file.material_id,
      }))
    },
    async resumeFile(input, signal) {
      let file = observe(
        await invoke(signal, () =>
          MaterialIngestService.readIngestFile({
            path: path(input.record.materialId!),
            signal,
          }),
        ),
        input.record,
      )
      if (receivedOriginal(file))
        return { state: "completed", identity: multipart(file) }
      if (["blocked", "failed"].includes(file.platform_status))
        throw new UploadError("part_rejected")
      if (
        !input.hasLocalFile &&
        !["completing", "result_unknown"].includes(
          file.operation_status ?? "idle",
        )
      )
        throw new UploadError("needs_reselect")
      file = observe(
        await invoke(signal, () =>
          MaterialIngestService.resumeIngestFile({
            path: path(file.material_id),
            body: ingestIdentity(file),
            signal,
          }),
        ),
        input.record,
      )
      if (receivedOriginal(file))
        return { state: "completed", identity: multipart(file) }
      if (file.operation_status === "completing") {
        const final = await finish(file, input.record, signal)
        return { state: "completed", identity: final }
      }
      if (
        file.temporary_storage_status === "waiting_capacity" ||
        file.operation_status === "initializing" ||
        file.temporary_storage_status === "reserved"
      )
        return { state: "waiting_capacity", retryAfterMs: 5000 }
      if (file.operation_status === "result_unknown")
        throw new UploadError("completion_unknown")
      if (file.temporary_storage_status !== "receiving")
        throw new UploadError("response_invalid")
      return { state: "receiving", identity: multipart(file) }
    },
    async listParts(input, signal) {
      const result = await invoke(signal, () =>
        MaterialIngestService.listIngestParts({
          path: path(input.identity.materialId),
          query: {
            ...requestIdentity(input.identity),
            upload_id: input.identity.uploadId,
            cursor: input.cursor,
            limit: 100,
          },
          signal,
        }),
      )
      return {
        identity: {
          ...input.identity,
          generation: result.generation,
          uploadId: result.upload_id,
          operationRevision: result.operation_revision,
        },
        parts: result.items.map((part) => ({
          partNumber: part.part_number,
          byteSize: part.byte_size,
          etag: part.etag,
        })),
        nextCursor: result.next_cursor ?? null,
      }
    },
    async signPart(input, signal) {
      if (!input.requestId) throw new UploadError("response_invalid")
      const result = await invoke(signal, () =>
        MaterialIngestService.signIngestParts({
          path: path(input.identity.materialId),
          body: {
            ...requestIdentity(input.identity),
            upload_id: input.identity.uploadId,
            part_numbers: [input.partNumber],
            request_id: input.requestId,
          },
          signal,
        }),
      )
      const expected = Math.min(
        input.identity.partSize,
        input.record.file.size -
          (input.partNumber - 1) * input.identity.partSize,
      )
      const part = result.items[0]
      if (
        result.generation !== input.identity.generation ||
        result.upload_id !== input.identity.uploadId ||
        result.operation_revision !== input.identity.operationRevision ||
        result.items.length !== 1 ||
        part?.part_number !== input.partNumber ||
        part.byte_size !== expected
      )
        throw new UploadError("response_invalid")
      return {
        url: part.url,
        permission: {
          id: part.permission_id,
          nonce: part.permission_nonce,
          revision: part.permission_revision,
          partNumber: part.part_number,
        },
      }
    },
    async completeFile(input, signal) {
      // Local parts are proof for browser resume. The server enumerates R2 itself.
      const file = await invoke(signal, () =>
        MaterialIngestService.completeIngestFile({
          path: path(input.identity.materialId),
          body: requestIdentity(input.identity),
          signal,
        }),
      )
      observe(file, input.record)
      if (
        file.generation !== input.identity.generation ||
        file.upload_id !== input.identity.uploadId
      )
        throw new UploadError("upload_identity_changed")
      return finish(file, input.record, signal)
    },
  }
  async function finish(
    initial: IngestFilePublic,
    record: UploadFileRecord,
    signal: AbortSignal,
  ): Promise<MultipartIdentity> {
    let file = initial
    // Each call is a bounded server page/claim; the browser never assumes one POST completed the object.
    for (let page = 0; page < 102; page++) {
      if (receivedOriginal(file)) return multipart(file)
      if (file.operation_status !== "completing")
        throw new UploadError("completion_unknown")
      await delay(250, signal)
      file = observe(
        await invoke(signal, () =>
          MaterialIngestService.completeIngestFile({
            path: path(file.material_id),
            body: ingestIdentity(file),
            signal,
          }),
        ),
        record,
      )
      if (
        file.generation !== initial.generation ||
        file.upload_id !== initial.upload_id ||
        file.operation_revision < initial.operation_revision
      )
        throw new UploadError("upload_identity_changed")
    }
    throw new UploadError("completion_unknown")
  }
}
