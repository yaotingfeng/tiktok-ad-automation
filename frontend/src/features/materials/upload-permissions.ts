/** Internal browser receipts, never signed URLs or a replacement public API schema. */
import {
  type MultipartIdentity,
  sameUpload,
  UploadError,
  type UploadScope,
} from "./upload-store"

export type PermissionIdentity = {
  id: string
  nonce: string
  revision: number
  partNumber: number
}
export type SignIntent = {
  scope: UploadScope
  clientIndex: number
  identity: MultipartIdentity
  requestId: string
  partNumber: number
}
export type PermissionOutcome = "completed" | "unused" | "unknown"
export type PermissionRecord = SignIntent & {
  state: "requested" | "signed" | "armed" | PermissionOutcome
  permission: PermissionIdentity | null
  etag: string | null
  acknowledged: boolean
}
export type PermissionCursor = IDBValidKey[]
const STORE = "permissions"
const key = (value: SignIntent): IDBValidKey[] => [
  ...scopeKey(value.scope),
  value.clientIndex,
  value.identity.generation,
  value.identity.uploadId,
  value.requestId,
  value.partNumber,
]
const scopeKey = (scope: UploadScope): IDBValidKey[] => [
  scope.tenantId,
  scope.bcId,
  scope.sessionId,
]
function valid(value: unknown): asserts value {
  if (!value) throw new UploadError("invalid_upload_metadata")
}
function uuid(value: string) {
  return /^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i.test(
    value,
  )
}
function cleanIntent(value: SignIntent): SignIntent {
  const { scope, identity } = value
  valid(
    [
      scope.tenantId,
      scope.bcId,
      scope.sessionId,
      identity.materialId,
      identity.uploadId,
    ].every((v) => typeof v === "string" && v.length > 0 && v.length <= 512),
  )
  valid(
    Number.isSafeInteger(value.clientIndex) &&
      value.clientIndex >= 0 &&
      value.clientIndex < 20000 &&
      uuid(value.requestId),
  )
  valid(
    Number.isSafeInteger(identity.generation) &&
      identity.generation > 0 &&
      Number.isSafeInteger(identity.operationRevision) &&
      identity.operationRevision >= 0,
  )
  valid(
    Number.isSafeInteger(identity.partSize) &&
      identity.partSize > 0 &&
      Number.isSafeInteger(identity.partCount) &&
      identity.partCount > 0 &&
      identity.partCount <= 10000,
  )
  valid(
    Number.isInteger(value.partNumber) &&
      value.partNumber >= 1 &&
      value.partNumber <= identity.partCount,
  )
  return {
    scope: {
      tenantId: scope.tenantId,
      bcId: scope.bcId,
      sessionId: scope.sessionId,
    },
    clientIndex: value.clientIndex,
    requestId: value.requestId,
    partNumber: value.partNumber,
    identity: {
      materialId: identity.materialId,
      generation: identity.generation,
      uploadId: identity.uploadId,
      operationRevision: identity.operationRevision,
      partSize: identity.partSize,
      partCount: identity.partCount,
    },
  }
}
function cleanPermission(
  value: PermissionIdentity,
  partNumber: number,
): PermissionIdentity {
  valid(
    uuid(value.id) &&
      uuid(value.nonce) &&
      Number.isSafeInteger(value.revision) &&
      value.revision >= 0 &&
      value.partNumber === partNumber,
  )
  return {
    id: value.id,
    nonce: value.nonce,
    revision: value.revision,
    partNumber,
  }
}
const request = <T>(value: IDBRequest<T>) =>
  new Promise<T>((resolve, reject) => {
    value.onsuccess = () => resolve(value.result)
    value.onerror = () => reject(new UploadError("storage_unavailable"))
  })

export class UploadPermissionLedger {
  private constructor(private readonly database: IDBDatabase) {}
  static open(
    name = "tiktok-upload-permissions-v1",
  ): Promise<UploadPermissionLedger> {
    return new Promise((resolve, reject) => {
      const opening = indexedDB.open(name, 1)
      opening.onupgradeneeded = () => opening.result.createObjectStore(STORE)
      opening.onerror = opening.onblocked = () =>
        reject(new UploadError("storage_unavailable"))
      opening.onsuccess = () => {
        opening.result.onversionchange = () => opening.result.close()
        resolve(new UploadPermissionLedger(opening.result))
      }
    })
  }
  close() {
    this.database.close()
  }
  private async transaction<T>(
    mode: IDBTransactionMode,
    work: (store: IDBObjectStore) => Promise<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    signal?.throwIfAborted()
    let transaction: IDBTransaction
    try {
      transaction = this.database.transaction(STORE, mode)
    } catch {
      throw new UploadError("storage_unavailable")
    }
    const abort = () => {
      try {
        transaction.abort()
      } catch {}
    }
    signal?.addEventListener("abort", abort, { once: true })
    const settled = new Promise<void>((resolve, reject) => {
      transaction.oncomplete = () => resolve()
      transaction.onabort = transaction.onerror = () =>
        reject(
          signal?.aborted
            ? signal.reason
            : new UploadError("storage_unavailable"),
        )
    })
    // Attach immediately so an interrupted async callback cannot cause an unhandled rejection.
    void settled.catch(() => {})
    try {
      const value = await work(transaction.objectStore(STORE))
      await settled
      signal?.throwIfAborted()
      return value
    } catch (error) {
      abort()
      await settled.catch(() => {})
      throw error
    } finally {
      signal?.removeEventListener("abort", abort)
    }
  }
  async begin(value: SignIntent, signal?: AbortSignal) {
    const intent = cleanIntent(value)
    return this.transaction(
      "readwrite",
      async (store) => {
        const existing = await request<PermissionRecord | undefined>(
          store.get(key(intent)),
        )
        if (existing) {
          valid(sameUpload(existing.identity, intent.identity))
          return existing
        }
        const record: PermissionRecord = {
          ...intent,
          state: "requested",
          permission: null,
          etag: null,
          acknowledged: false,
        }
        await request(store.add(record, key(intent)))
        return record
      },
      signal,
    )
  }
  private async change(
    intent: SignIntent,
    update: (row: PermissionRecord) => void,
    signal?: AbortSignal,
  ) {
    cleanIntent(intent)
    return this.transaction(
      "readwrite",
      async (store) => {
        const row = await request<PermissionRecord | undefined>(
          store.get(key(intent)),
        )
        valid(row && sameUpload(row.identity, intent.identity))
        update(row)
        await request(store.put(row, key(intent)))
        return row
      },
      signal,
    )
  }
  async signed(
    intent: SignIntent,
    permission: PermissionIdentity,
    signal?: AbortSignal,
  ) {
    const clean = cleanPermission(permission, intent.partNumber)
    return this.change(
      intent,
      (row) => {
        if (row.permission) {
          valid(JSON.stringify(row.permission) === JSON.stringify(clean))
          return
        }
        valid(row.state === "requested")
        row.permission = clean
        row.state = "signed"
      },
      signal,
    )
  }
  async armed(intent: SignIntent, signal?: AbortSignal) {
    return this.change(
      intent,
      (row) => {
        valid(row.state === "signed")
        row.state = "armed"
      },
      signal,
    )
  }
  /** Called only by the live scheduler before it invokes signPart/PUT. */
  async unusedBeforeSend(intent: SignIntent, signal?: AbortSignal) {
    return this.change(
      intent,
      (row) => {
        valid(["requested", "signed", "armed"].includes(row.state))
        row.state = "unused"
        row.acknowledged = row.permission === null
      },
      signal,
    )
  }
  async settled(
    intent: SignIntent,
    outcome: PermissionOutcome,
    etag?: string,
    signal?: AbortSignal,
  ) {
    valid(["completed", "unused", "unknown"].includes(outcome))
    return this.change(
      intent,
      (row) => {
        if (row.state === "unknown") return // Sticky: another permission or a late observation cannot clear it.
        if (row.state === outcome) {
          if (outcome === "completed") valid(row.etag === etag)
          return
        }
        valid(
          row.state === (outcome === "unused" ? "signed" : "armed") ||
            (outcome === "unknown" && row.state === "signed"),
        )
        if (outcome === "completed")
          valid(
            typeof etag === "string" &&
              etag.trim().length > 0 &&
              etag.length <= 256 &&
              [...etag].every((char) => char.charCodeAt(0) >= 32),
          )
        row.state = outcome
        row.etag = outcome === "completed" ? etag! : null
      },
      signal,
    )
  }
  async acknowledge(
    intent: SignIntent,
    permissionId: string,
    signal?: AbortSignal,
  ) {
    return this.change(
      intent,
      (row) => {
        valid(
          row.permission?.id === permissionId &&
            ["completed", "unused", "unknown"].includes(row.state),
        )
        row.acknowledged = true
      },
      signal,
    )
  }
  async page(
    scope: UploadScope,
    after?: PermissionCursor | null,
    limit = 100,
    clientIndex?: number,
  ): Promise<{
    items: PermissionRecord[]
    nextCursor: PermissionCursor | null
  }> {
    valid(Number.isInteger(limit) && limit >= 1 && limit <= 100)
    if (clientIndex !== undefined)
      valid(
        Number.isInteger(clientIndex) &&
          clientIndex >= 0 &&
          clientIndex < 20000,
      )
    const prefix = [
      ...scopeKey(scope),
      ...(clientIndex === undefined ? [] : [clientIndex]),
    ]
    if (after) valid(prefix.every((v, i) => v === after[i]))
    return this.transaction("readonly", async (store) => {
      const range = IDBKeyRange.bound(
        after ?? prefix,
        [...prefix, []],
        !!after,
        true,
      )
      const [rows, keys] = await Promise.all([
        request<PermissionRecord[]>(store.getAll(range, limit + 1)),
        request(store.getAllKeys(range, limit + 1)),
      ])
      return {
        items: rows.slice(0, limit),
        nextCursor:
          rows.length > limit ? (keys[limit - 1] as PermissionCursor) : null,
      }
    })
  }
}
