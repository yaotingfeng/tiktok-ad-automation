/** Browser metadata only. All writes project explicit fields; never persist API responses. */
export type UploadScope = { tenantId: string; bcId: string; sessionId: string }
/** Stable browser key; serverSessionId is the actual API session after its receipt. */
export type UploadImport = UploadScope & {
  requestId: string
  fileCount: number
  totalBytes: number
  serverSessionId: string | null
  metadataReady: boolean
  createdAt: number
}
export type LocalFileIdentity = {
  name: string
  size: number
  lastModified: number
  type: string
}
export type MultipartIdentity = {
  materialId: string
  generation: number
  uploadId: string
  /** Ownership/multipart fence, NOT a received-byte or progress counter. */
  operationRevision: number
  partSize: number
  partCount: number
}
export type UploadFileState =
  | "selected"
  | "registered"
  | "transferring"
  | "completed"
export type UploadFileRecord = UploadScope & {
  clientIndex: number
  registrationRequestId: string
  file: LocalFileIdentity
  materialId: string | null
  upload: MultipartIdentity | null
  state: UploadFileState
}
export type LocalPart = {
  partNumber: number
  byteSize: number
  sha256: string
  /** A prepared digest is committed BEFORE any possible storage PUT. */
  state: "prepared" | "confirmed"
  etag: string | null
}
export type UploadErrorCode =
  | "storage_unavailable"
  | "invalid_upload_metadata"
  | "upload_identity_changed"
  | "registration_conflict"
  | "registration_required"
  | "storage_backpressure"
  | "needs_reselect"
  | "wrong_file"
  | "ambiguous_file"
  | "needs_new_generation"
  | "part_unavailable"
  | "part_rejected"
  | "part_signature_expired"
  | "response_invalid"
  | "completion_unknown"
  | "permission_denied"
  | "transfer_unavailable"

export class UploadError extends Error {
  constructor(
    public readonly code: UploadErrorCode,
    public readonly retryAfterMs?: number,
  ) {
    super(code)
    this.name = "UploadError"
  }
}

const IMPORTS = "imports"
const FILES = "files"
const PARTS = "parts"
const scopeFields = ["tenantId", "bcId", "sessionId"]
const fileFields = [...scopeFields, "clientIndex"]
const partFields = [
  ...fileFields,
  "generation",
  "uploadId",
  "operationRevision",
  "partNumber",
]
const scopeKey = (scope: UploadScope): IDBValidKey[] => [
  scope.tenantId,
  scope.bcId,
  scope.sessionId,
]
const fileKey = (scope: UploadScope, index: number): IDBValidKey[] => [
  ...scopeKey(scope),
  index,
]
const partPrefix = (
  scope: UploadScope,
  index: number,
  upload: MultipartIdentity,
): IDBValidKey[] => [
  ...fileKey(scope, index),
  upload.generation,
  upload.uploadId,
  upload.operationRevision,
]
const range = (prefix: IDBValidKey[]) =>
  IDBKeyRange.bound(prefix, [...prefix, []])

function assert(value: unknown): asserts value {
  if (!value) throw new UploadError("invalid_upload_metadata")
}
function integer(value: number, minimum = 0) {
  return Number.isSafeInteger(value) && value >= minimum
}
function label(value: string) {
  return (
    typeof value === "string" && value.trim().length > 0 && value.length <= 1024
  )
}
function cleanScope(scope: UploadScope): UploadScope {
  assert(label(scope.tenantId) && label(scope.bcId) && label(scope.sessionId))
  return {
    tenantId: scope.tenantId,
    bcId: scope.bcId,
    sessionId: scope.sessionId,
  }
}
export function fileIdentity(file: File): LocalFileIdentity {
  return {
    name: file.name,
    size: file.size,
    type: file.type,
    lastModified: file.lastModified,
  }
}
export function sameFile(a: LocalFileIdentity, b: LocalFileIdentity) {
  return (
    a.name === b.name &&
    a.size === b.size &&
    a.type === b.type &&
    a.lastModified === b.lastModified
  )
}
export function sameUpload(a: MultipartIdentity | null, b: MultipartIdentity) {
  return (
    !!a &&
    a.materialId === b.materialId &&
    a.generation === b.generation &&
    a.uploadId === b.uploadId &&
    a.operationRevision === b.operationRevision &&
    a.partSize === b.partSize &&
    a.partCount === b.partCount
  )
}
function cleanUpload(upload: MultipartIdentity): MultipartIdentity {
  assert(
    label(upload.materialId) &&
      label(upload.uploadId) &&
      integer(upload.generation, 1) &&
      integer(upload.operationRevision) &&
      integer(upload.partSize, 1) &&
      integer(upload.partCount, 1) &&
      upload.partCount <= 10_000,
  )
  return {
    materialId: upload.materialId,
    generation: upload.generation,
    uploadId: upload.uploadId,
    operationRevision: upload.operationRevision,
    partSize: upload.partSize,
    partCount: upload.partCount,
  }
}
function cleanFile(row: UploadFileRecord): UploadFileRecord {
  assert(
    integer(row.clientIndex) &&
      label(row.registrationRequestId) &&
      label(row.file.name) &&
      integer(row.file.size, 1) &&
      integer(row.file.lastModified) &&
      typeof row.file.type === "string" &&
      row.file.type.length <= 255,
  )
  assert(row.materialId === null || label(row.materialId))
  assert(
    ["selected", "registered", "transferring", "completed"].includes(row.state),
  )
  return {
    ...cleanScope(row),
    clientIndex: row.clientIndex,
    registrationRequestId: row.registrationRequestId,
    file: {
      name: row.file.name,
      size: row.file.size,
      lastModified: row.file.lastModified,
      type: row.file.type,
    },
    materialId: row.materialId,
    upload: row.upload ? cleanUpload(row.upload) : null,
    state: row.state,
  }
}
function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(new UploadError("storage_unavailable"))
  })
}

export class UploadStore {
  private constructor(private readonly db: IDBDatabase) {}
  get name() {
    return this.db.name
  }
  static open(name = "tiktok-material-transfers-v1"): Promise<UploadStore> {
    return new Promise((resolve, reject) => {
      let settled = false
      try {
        const request = indexedDB.open(name, 2)
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains(FILES))
            request.result.createObjectStore(FILES, { keyPath: fileFields })
          if (!request.result.objectStoreNames.contains(PARTS))
            request.result.createObjectStore(PARTS, { keyPath: partFields })
          const imports = request.result.createObjectStore(IMPORTS, {
            keyPath: scopeFields,
          })
          imports.createIndex(
            "server_scope",
            ["tenantId", "bcId", "serverSessionId"],
            { unique: true },
          )
        }
        request.onerror = request.onblocked = () => {
          settled = true
          reject(new UploadError("storage_unavailable"))
        }
        request.onsuccess = () => {
          if (settled) {
            request.result.close()
            return
          }
          request.result.onversionchange = () => request.result.close()
          resolve(new UploadStore(request.result))
        }
      } catch {
        reject(new UploadError("storage_unavailable"))
      }
    })
  }
  close() {
    this.db.close()
  }
  private async transaction<T>(
    names: string[],
    mode: IDBTransactionMode,
    work: (tx: IDBTransaction) => Promise<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    signal?.throwIfAborted()
    return new Promise((resolve, reject) => {
      let tx: IDBTransaction
      try {
        tx = this.db.transaction(names, mode)
      } catch {
        reject(new UploadError("storage_unavailable"))
        return
      }
      let value: T
      let failure: unknown
      const abort = () => {
        try {
          tx.abort()
        } catch {
          /* already complete */
        }
      }
      signal?.addEventListener("abort", abort, { once: true })
      const cleanup = () => signal?.removeEventListener("abort", abort)
      tx.oncomplete = () => {
        cleanup()
        resolve(value)
      }
      tx.onabort = () => {
        cleanup()
        reject(
          signal?.aborted
            ? signal.reason
            : (failure ?? new UploadError("storage_unavailable")),
        )
      }
      work(tx)
        .then((next) => {
          value = next
        })
        .catch((error: unknown) => {
          failure = error
          abort()
        })
    })
  }
  async putImport(value: UploadImport, signal?: AbortSignal) {
    const clean: UploadImport = {
      ...cleanScope(value),
      requestId: value.requestId,
      fileCount: value.fileCount,
      totalBytes: value.totalBytes,
      serverSessionId: value.serverSessionId,
      metadataReady: value.metadataReady,
      createdAt: value.createdAt,
    }
    assert(
      label(clean.requestId) &&
        integer(clean.fileCount, 1) &&
        clean.fileCount <= 20000 &&
        integer(clean.totalBytes, 1) &&
        integer(clean.createdAt) &&
        typeof clean.metadataReady === "boolean" &&
        (clean.serverSessionId === null || label(clean.serverSessionId)),
    )
    await this.transaction(
      [IMPORTS],
      "readwrite",
      async (tx) => {
        const store = tx.objectStore(IMPORTS)
        const old = await result<UploadImport | undefined>(
          store.get(scopeKey(clean)),
        )
        if (old) {
          if (
            old.requestId !== clean.requestId ||
            old.fileCount !== clean.fileCount ||
            old.totalBytes !== clean.totalBytes ||
            old.createdAt !== clean.createdAt ||
            (old.serverSessionId &&
              clean.serverSessionId &&
              old.serverSessionId !== clean.serverSessionId)
          )
            throw new UploadError("registration_conflict")
          clean.serverSessionId ??= old.serverSessionId
          clean.metadataReady ||= old.metadataReady
        }
        await result(store.put(clean))
      },
      signal,
    )
  }
  async getImport(scope: UploadScope): Promise<UploadImport | undefined> {
    return this.transaction([IMPORTS], "readonly", (tx) =>
      result(tx.objectStore(IMPORTS).get(scopeKey(cleanScope(scope)))),
    )
  }
  async findImport(
    tenantId: string,
    bcId: string,
    serverSessionId: string,
  ): Promise<UploadImport | undefined> {
    assert(label(tenantId) && label(bcId) && label(serverSessionId))
    return this.transaction([IMPORTS], "readonly", (tx) =>
      result(
        tx
          .objectStore(IMPORTS)
          .index("server_scope")
          .get([tenantId, bcId, serverSessionId]),
      ),
    )
  }
  async listImports(
    tenantId: string,
    bcId: string,
    after: string | null = null,
    limit = 100,
  ): Promise<UploadImport[]> {
    assert(label(tenantId) && label(bcId) && integer(limit, 1) && limit <= 100)
    return this.transaction([IMPORTS], "readonly", (tx) =>
      result(
        tx
          .objectStore(IMPORTS)
          .getAll(
            IDBKeyRange.bound(
              [tenantId, bcId, after ?? ""],
              [tenantId, bcId, []],
              true,
            ),
            limit,
          ),
      ),
    )
  }
  async putFiles(rows: readonly UploadFileRecord[], signal?: AbortSignal) {
    assert(rows.length > 0 && rows.length <= 200)
    const clean = rows.map(cleanFile)
    await this.transaction(
      [FILES],
      "readwrite",
      async (tx) => {
        const store = tx.objectStore(FILES)
        for (const row of clean) {
          const old = await result<UploadFileRecord | undefined>(
            store.get(fileKey(row, row.clientIndex)),
          )
          if (old) {
            if (
              !sameFile(old.file, row.file) ||
              old.registrationRequestId !== row.registrationRequestId
            )
              throw new UploadError("registration_conflict")
          } else {
            await result(store.add(row))
          }
        }
      },
      signal,
    )
  }
  async getFile(
    scope: UploadScope,
    index: number,
  ): Promise<UploadFileRecord | undefined> {
    cleanScope(scope)
    assert(integer(index))
    return this.transaction([FILES], "readonly", (tx) =>
      result(tx.objectStore(FILES).get(fileKey(scope, index))),
    )
  }
  async listFiles(
    scope: UploadScope,
    after = -1,
    limit = 100,
  ): Promise<UploadFileRecord[]> {
    cleanScope(scope)
    assert(integer(limit, 1) && limit <= 100 && integer(after + 1))
    return this.transaction([FILES], "readonly", (tx) =>
      result(
        tx
          .objectStore(FILES)
          .getAll(
            IDBKeyRange.bound(
              [...scopeKey(scope), after],
              [...scopeKey(scope), []],
              true,
            ),
            limit,
          ),
      ),
    )
  }
  async bindMaterials(
    scope: UploadScope,
    rows: readonly { clientIndex: number; materialId: string }[],
    signal?: AbortSignal,
  ) {
    cleanScope(scope)
    assert(rows.length > 0 && rows.length <= 200)
    await this.transaction(
      [FILES],
      "readwrite",
      async (tx) => {
        const store = tx.objectStore(FILES)
        for (const binding of rows) {
          assert(integer(binding.clientIndex) && label(binding.materialId))
          const row = await result<UploadFileRecord | undefined>(
            store.get(fileKey(scope, binding.clientIndex)),
          )
          if (
            !row ||
            (row.materialId !== null && row.materialId !== binding.materialId)
          )
            throw new UploadError("registration_conflict")
          row.materialId = binding.materialId
          if (row.state === "selected") row.state = "registered"
          await result(store.put(cleanFile(row)))
        }
      },
      signal,
    )
  }
  async bindUpload(
    scope: UploadScope,
    index: number,
    upload: MultipartIdentity,
    signal?: AbortSignal,
  ) {
    const clean = cleanUpload(upload)
    await this.transaction(
      [FILES],
      "readwrite",
      async (tx) => {
        const store = tx.objectStore(FILES)
        const row = await result<UploadFileRecord | undefined>(
          store.get(fileKey(cleanScope(scope), index)),
        )
        if (
          !row ||
          row.materialId !== clean.materialId ||
          row.state === "completed" ||
          (row.upload &&
            (row.upload.generation > clean.generation ||
              (row.upload.generation === clean.generation &&
                (row.upload.uploadId !== clean.uploadId ||
                  row.upload.operationRevision > clean.operationRevision ||
                  row.upload.partSize !== clean.partSize ||
                  row.upload.partCount !== clean.partCount))))
        )
          throw new UploadError("upload_identity_changed")
        assert(clean.partCount === Math.ceil(row.file.size / clean.partSize))
        row.upload = clean
        row.state = "transferring"
        await result(store.put(cleanFile(row)))
      },
      signal,
    )
  }
  async putPart(
    scope: UploadScope,
    index: number,
    upload: MultipartIdentity,
    part: LocalPart,
    signal?: AbortSignal,
  ) {
    assert(
      integer(part.partNumber, 1) &&
        part.partNumber <= upload.partCount &&
        integer(part.byteSize, 1) &&
        /^[a-f0-9]{64}$/.test(part.sha256) &&
        ["prepared", "confirmed"].includes(part.state) &&
        (part.state === "prepared"
          ? part.etag === null
          : label(part.etag ?? "")),
    )
    await this.transaction(
      [FILES, PARTS],
      "readwrite",
      async (tx) => {
        const row = await result<UploadFileRecord | undefined>(
          tx.objectStore(FILES).get(fileKey(cleanScope(scope), index)),
        )
        if (
          !row ||
          row.state === "completed" ||
          !sameUpload(row.upload, upload)
        )
          throw new UploadError("upload_identity_changed")
        assert(
          part.byteSize ===
            Math.min(
              upload.partSize,
              row.file.size - (part.partNumber - 1) * upload.partSize,
            ),
        )
        await result(
          tx.objectStore(PARTS).put({
            ...cleanScope(scope),
            clientIndex: index,
            generation: upload.generation,
            uploadId: upload.uploadId,
            operationRevision: upload.operationRevision,
            partNumber: part.partNumber,
            byteSize: part.byteSize,
            sha256: part.sha256,
            state: part.state,
            etag: part.etag,
          }),
        )
      },
      signal,
    )
  }
  async listParts(
    scope: UploadScope,
    index: number,
    upload: MultipartIdentity,
  ): Promise<LocalPart[]> {
    cleanScope(scope)
    cleanUpload(upload)
    return this.transaction([PARTS], "readonly", (tx) =>
      result(
        tx.objectStore(PARTS).getAll(range(partPrefix(scope, index, upload))),
      ),
    )
  }
  async markCompleted(
    scope: UploadScope,
    index: number,
    upload: MultipartIdentity,
    signal?: AbortSignal,
    confirmed: MultipartIdentity = upload,
  ) {
    cleanUpload(confirmed)
    if (
      !sameUpload(
        { ...confirmed, operationRevision: upload.operationRevision },
        upload,
      ) ||
      confirmed.operationRevision < upload.operationRevision
    )
      throw new UploadError("upload_identity_changed")
    await this.transaction(
      [FILES, PARTS],
      "readwrite",
      async (tx) => {
        const store = tx.objectStore(FILES)
        const row = await result<UploadFileRecord | undefined>(
          store.get(fileKey(cleanScope(scope), index)),
        )
        if (!row?.materialId || !sameUpload(row.upload, upload))
          throw new UploadError("upload_identity_changed")
        row.upload = cleanUpload(confirmed)
        row.state = "completed"
        await result(store.put(cleanFile(row)))
        await result(tx.objectStore(PARTS).delete(range(fileKey(scope, index))))
      },
      signal,
    )
  }
}
