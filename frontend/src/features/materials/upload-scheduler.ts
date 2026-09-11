/** Adapter boundary, not a second HTTP API schema. UI/generated-client integration lives outside this module. */
import type {
  PermissionCursor,
  PermissionIdentity,
  PermissionOutcome,
  PermissionRecord,
  SignIntent,
  UploadPermissionLedger,
} from "./upload-permissions"
import {
  fileIdentity,
  type LocalPart,
  type MultipartIdentity,
  sameFile,
  sameUpload,
  UploadError,
  type UploadErrorCode,
  type UploadFileRecord,
  type UploadScope,
  type UploadStore,
} from "./upload-store"

export const INITIAL_PART_BYTES = 16 * 1024 * 1024
// 单文件上限与后端 R2/URL 上传默认值一致；仍按分片读取，避免整文件占用内存。
export const MAX_BROWSER_FILE_BYTES = 1024 ** 3
export type TransferRequest = { scope: UploadScope; record: UploadFileRecord }
export type MultipartRequest = TransferRequest & { identity: MultipartIdentity }
export type RemotePart = {
  partNumber: number
  byteSize: number
  etag: string
  sha256?: string
}
export type ResumeResult =
  | { state: "waiting_capacity"; retryAfterMs: number }
  | { state: "receiving"; identity: MultipartIdentity }
  | { state: "completed"; identity: MultipartIdentity }
export type TransferCallbacks = {
  registerChunk(
    input: {
      scope: UploadScope
      requestId: string
      files: readonly { clientIndex: number; file: UploadFileRecord["file"] }[]
    },
    signal: AbortSignal,
  ): Promise<readonly { clientIndex: number; materialId: string }[]>
  /** Reserve/reauthorize through the application; never sign before receiving capacity. */
  resumeFile(
    input: TransferRequest & { hasLocalFile: boolean },
    signal: AbortSignal,
  ): Promise<ResumeResult>
  listParts(
    input: MultipartRequest & { cursor: string | null; limit: 100 },
    signal: AbortSignal,
  ): Promise<{
    identity: MultipartIdentity
    parts: readonly RemotePart[]
    nextCursor: string | null
  }>
  signPart(
    input: MultipartRequest & { partNumber: number; requestId?: string },
    signal: AbortSignal,
  ): Promise<{ url: string; permission?: PermissionIdentity }>
  putPart?(
    input: { url: string; blob: Blob },
    signal: AbortSignal,
  ): Promise<{ etag: string }>
  /** Application rechecks identity, current parts and object completion. A lost reply is NOT success. */
  completeFile(
    input: MultipartRequest & { parts: readonly RemotePart[] },
    signal: AbortSignal,
  ): Promise<MultipartIdentity>
}
export type TransferProgress = {
  clientIndex: number
  state:
    | "registered"
    | "transferring"
    | "completed"
    | "issue"
    | "waiting_capacity"
  receivedBytes: number
  errorCode?: UploadErrorCode
  retryAfterMs?: number
}
export type TransferRunResult = {
  completed: number
  issues: number
  retryAfterMs?: number
  stopped?: boolean
}
export type PermissionCallbacks = {
  ledger: UploadPermissionLedger
  readSignPermission(
    input: MultipartRequest & { requestId: string; partNumber: number },
    signal: AbortSignal,
  ): Promise<{
    permission: PermissionIdentity
    outcome: "signed" | PermissionOutcome
  } | null>
  acknowledgeReceipts(
    input: MultipartRequest & {
      receipts: readonly {
        permission: PermissionIdentity
        outcome: PermissionOutcome
        etag: string | null
      }[]
    },
    signal: AbortSignal,
  ): Promise<readonly string[]>
}
export type CancellationDrain = {
  completed: number
  unused: number
  unknown: number
  unresolved: number
}
export type SchedulerOptions = {
  scope: UploadScope
  store: UploadStore
  callbacks: TransferCallbacks
  signal: AbortSignal
  maxFiles?: number
  maxParts?: number
  retryBaseMs?: number
  onProgress?: (event: TransferProgress) => void
  /** Required for certified cancellation. Older adapters cannot certify a drain. */
  permissions?: PermissionCallbacks
}

function valid(condition: unknown): asserts condition {
  if (!condition) throw new UploadError("response_invalid")
}
function retryDelay(value: number) {
  valid(Number.isFinite(value) && value >= 0)
  return Math.max(1000, value)
}
async function sha256(blob: Blob) {
  const bytes = await blob.arrayBuffer()
  const hash = await crypto.subtle.digest("SHA-256", bytes)
  return Array.from(new Uint8Array(hash), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("")
}
function sleep(ms: number, signal: AbortSignal) {
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

/** No application bearer/cookies/referrer and no forbidden Content-Length header.
 * The browser derives Content-Length from this exact, signed-layout Blob slice. */
export async function putSignedPart(
  input: { url: string; blob: Blob },
  signal: AbortSignal,
): Promise<{ etag: string }> {
  const url = new URL(input.url)
  valid(url.protocol === "https:" && !url.username && !url.password)
  let response: Response
  try {
    response = await fetch(url, {
      method: "PUT",
      body: input.blob,
      signal,
      credentials: "omit",
      referrerPolicy: "no-referrer",
      redirect: "error",
    })
  } catch {
    signal.throwIfAborted()
    throw new UploadError("part_unavailable")
  }
  if (response.status === 403) throw new UploadError("part_signature_expired")
  if (
    response.status === 408 ||
    response.status === 429 ||
    response.status >= 500
  )
    throw new UploadError("part_unavailable")
  if (!response.ok) throw new UploadError("part_rejected")
  const etag = response.headers.get("ETag")
  if (!etag?.trim()) throw new UploadError("part_unavailable") // May have succeeded: ListParts before retry.
  return { etag }
}

export class UploadScheduler {
  private readonly scope: UploadScope
  private readonly files = new Map<number, File>() // File refs only; never materialize the full import as bytes.
  private readonly maxFiles: number
  private readonly maxParts: number
  private readonly retryBaseMs: number
  private readonly progressAt = new Map<number, number>()
  private controller: AbortController | null = null
  private running: Promise<TransferRunResult> | null = null
  private stopRequested = false
  private readonly cancelScope = () => {
    this.pause()
    this.files.clear()
  }
  constructor(private readonly options: SchedulerOptions) {
    this.scope = Object.freeze({ ...options.scope })
    this.maxFiles = options.maxFiles ?? 4
    this.maxParts = options.maxParts ?? 2
    this.retryBaseMs = options.retryBaseMs ?? 1000
    valid(
      Number.isInteger(this.maxFiles) &&
        this.maxFiles >= 1 &&
        this.maxFiles <= 4 &&
        Number.isInteger(this.maxParts) &&
        this.maxParts >= 1 &&
        this.maxParts <= 2 &&
        Number.isFinite(this.retryBaseMs) &&
        this.retryBaseMs >= 10,
    )
    options.signal.addEventListener("abort", this.cancelScope, { once: true })
  }
  pause() {
    this.controller?.abort(new DOMException("Transfer paused", "AbortError"))
  }
  /** Stop scheduling, keep current HTTP alive, then publish its durable outcomes. */
  async drainForCancellation(): Promise<CancellationDrain> {
    if (!this.options.permissions)
      throw new UploadError("permission_receipts_unavailable")
    this.stopRequested = true
    if (this.running) await this.running
    const signal = this.start()
    this.stopRequested = true
    try {
      return await this.recoverPermissionReceipts(signal)
    } finally {
      this.controller = null
    }
  }
  dispose() {
    this.pause()
    this.files.clear()
    this.options.signal.removeEventListener("abort", this.cancelScope)
  }
  private start() {
    this.options.signal.throwIfAborted()
    if (this.controller) throw new UploadError("transfer_unavailable")
    this.controller = new AbortController()
    this.stopRequested = false
    return this.controller.signal
  }
  private check(signal: AbortSignal) {
    this.options.signal.throwIfAborted()
    signal.throwIfAborted()
  }
  private async call<T>(
    work: () => Promise<T>,
    signal: AbortSignal,
  ): Promise<T> {
    this.check(signal)
    try {
      const result = await work()
      this.check(signal)
      return result
    } catch (error) {
      this.check(signal)
      throw error instanceof UploadError
        ? error
        : new UploadError("transfer_unavailable")
    }
  }
  private notify(event: TransferProgress, signal: AbortSignal) {
    this.check(signal)
    const now = performance.now()
    if (
      event.state === "transferring" &&
      now - (this.progressAt.get(event.clientIndex) ?? -Infinity) < 100
    )
      return
    this.progressAt.set(event.clientIndex, now)
    // Observer errors cannot change persisted transfer facts or cause a replay.
    try {
      this.options.onProgress?.(event)
    } catch {
      /* adapter owns UI errors */
    }
  }
  private async *records(signal: AbortSignal) {
    let after = -1
    while (true) {
      this.check(signal)
      const rows = await this.options.store.listFiles(this.scope, after, 100)
      this.check(signal)
      if (!rows.length) return
      for (const row of rows) {
        this.check(signal)
        yield row
      }
      after = rows[rows.length - 1].clientIndex
    }
  }
  private async registerChunk(
    rows: readonly UploadFileRecord[],
    signal: AbortSignal,
  ) {
    if (this.stopRequested || rows.every((row) => row.materialId)) return
    valid(
      rows.length > 0 &&
        rows.length <= 200 &&
        rows.every(
          (row) =>
            !row.materialId &&
            row.registrationRequestId === rows[0].registrationRequestId,
        ),
    )
    const bindings = await this.call(
      () =>
        this.options.callbacks.registerChunk(
          {
            scope: this.scope,
            requestId: rows[0].registrationRequestId,
            files: rows.map((row) => ({
              clientIndex: row.clientIndex,
              file: row.file,
            })),
          },
          signal,
        ),
      signal,
    )
    const expected = new Set(rows.map((row) => row.clientIndex))
    valid(bindings.length === expected.size)
    for (const binding of bindings) valid(expected.delete(binding.clientIndex))
    await this.options.store.bindMaterials(this.scope, bindings, signal)
    for (const row of rows)
      this.notify(
        { clientIndex: row.clientIndex, state: "registered", receivedBytes: 0 },
        signal,
      )
  }
  /** Persist the complete manifest before the parent-session HTTP request.
   * Bounded IDB transactions retain no File/Blob; in-memory references stay scoped. */
  async stageFiles(files: readonly File[]) {
    valid(files.length > 0 && files.length <= 20_000)
    for (const file of files)
      valid(file.size > 0 && file.size <= MAX_BROWSER_FILE_BYTES)
    const signal = this.start()
    try {
      for (let start = 0; start < files.length; start += 200) {
        this.check(signal)
        const previous = await this.options.store.getFile(this.scope, start)
        const requestId = previous?.registrationRequestId ?? crypto.randomUUID()
        const rows = files.slice(start, start + 200).map(
          (file, offset): UploadFileRecord => ({
            ...this.scope,
            clientIndex: start + offset,
            registrationRequestId: requestId,
            file: fileIdentity(file),
            materialId: null,
            upload: null,
            state: "selected",
          }),
        )
        await this.options.store.putFiles(rows, signal)
        for (const row of rows)
          this.files.set(row.clientIndex, files[row.clientIndex])
      }
    } finally {
      this.controller = null
    }
  }
  /** Same ordered selection retries stable chunk keys; all metadata is durable first. */
  async registerFiles(files: readonly File[]) {
    await this.stageFiles(files)
    const signal = this.start()
    try {
      await this.retryRegistrations(signal)
    } finally {
      this.controller = null
    }
  }
  private async retryRegistrations(signal: AbortSignal) {
    let chunk: UploadFileRecord[] = []
    for await (const row of this.records(signal)) {
      if (this.stopRequested) return
      if (
        chunk.length &&
        row.registrationRequestId !== chunk[0].registrationRequestId
      ) {
        await this.registerChunk(chunk, signal)
        chunk = []
      }
      chunk.push(row)
      valid(chunk.length <= 200)
    }
    if (chunk.length) await this.registerChunk(chunk, signal)
  }
  /** Refresh has lost local file authority. All candidate identities and saved SHA256 proofs must match. */
  async reselect(clientIndex: number, candidates: readonly File[]) {
    const signal = this.start()
    try {
      const row = await this.options.store.getFile(this.scope, clientIndex)
      valid(row)
      await this.attach(
        row,
        this.selection(candidates).get(this.selectionKey(row.file)) ?? [],
        signal,
      )
    } finally {
      this.controller = null
    }
  }
  private selectionKey(file: UploadFileRecord["file"]) {
    return JSON.stringify([file.name, file.size, file.lastModified, file.type])
  }
  private selection(files: readonly File[]) {
    valid(files.length <= 20_000)
    const index = new Map<string, File[]>()
    for (const file of files) {
      const key = this.selectionKey(fileIdentity(file)),
        matches = index.get(key)
      if (matches) matches.push(file)
      else index.set(key, [file])
    }
    return index
  }
  /** Whole-dialog reselect uses one identity Map plus scoped pages, never N×selection.find(). */
  async reselectFiles(
    files: readonly File[],
  ): Promise<{ matched: number; issues: number }> {
    const signal = this.start()
    const summary = { matched: 0, issues: 0 }
    try {
      const candidates = this.selection(files)
      const manifestCounts = new Map<string, number>()
      for await (const row of this.records(signal)) {
        if (row.state !== "completed") {
          const key = this.selectionKey(row.file)
          manifestCounts.set(key, (manifestCounts.get(key) ?? 0) + 1)
        }
      }
      for await (const row of this.records(signal)) {
        if (row.state === "completed") continue
        try {
          if ((manifestCounts.get(this.selectionKey(row.file)) ?? 0) > 1) {
            this.files.delete(row.clientIndex)
            throw new UploadError("ambiguous_file")
          }
          await this.attach(
            row,
            candidates.get(this.selectionKey(row.file)) ?? [],
            signal,
          )
          summary.matched++
        } catch (error) {
          this.check(signal)
          if (!(error instanceof UploadError)) throw error
          summary.issues++
          this.notify(
            {
              clientIndex: row.clientIndex,
              state: "issue",
              receivedBytes: 0,
              errorCode: error.code,
            },
            signal,
          )
        }
      }
      return summary
    } finally {
      this.controller = null
      this.progressAt.clear()
    }
  }
  private async attach(
    row: UploadFileRecord,
    matches: readonly File[],
    signal: AbortSignal,
  ) {
    this.check(signal)
    if (row.state === "completed") return
    this.files.delete(row.clientIndex)
    if (matches.length > 1) throw new UploadError("ambiguous_file")
    if (!matches.length) throw new UploadError("wrong_file")
    const file = matches[0]
    if (row.upload) {
      for (const part of await this.options.store.listParts(
        this.scope,
        row.clientIndex,
        row.upload,
      )) {
        this.check(signal)
        if (
          (await sha256(
            file.slice(
              (part.partNumber - 1) * row.upload.partSize,
              part.partNumber * row.upload.partSize,
            ),
          )) !== part.sha256
        )
          throw new UploadError("wrong_file")
      }
    }
    this.check(signal)
    this.files.set(row.clientIndex, file)
  }
  run(): Promise<TransferRunResult> {
    if (this.running) return this.running
    const signal = this.start()
    this.running = this.drive(signal).finally(() => {
      this.running = null
      this.controller = null
      this.progressAt.clear()
    })
    return this.running
  }
  resume() {
    return this.run()
  }
  private async drive(signal: AbortSignal): Promise<TransferRunResult> {
    await this.retryRegistrations(signal)
    const rows = this.records(signal)
    const summary: TransferRunResult = { completed: 0, issues: 0 }
    const worker = async () => {
      while (!summary.retryAfterMs && !this.stopRequested) {
        this.check(signal)
        const next = await rows.next()
        if (next.done || summary.retryAfterMs || this.stopRequested) return
        const row = next.value
        if (row.state === "completed") continue
        try {
          if (this.options.permissions) {
            const receipts = await this.recoverPermissionReceipts(
              signal,
              row.clientIndex,
            )
            if (receipts.unresolved)
              throw new UploadError("transfer_unavailable")
          }
          if (this.stopRequested) return
          await this.transfer(row, signal)
          if (!this.stopRequested) summary.completed++
        } catch (error) {
          this.check(signal)
          if (this.stopRequested) return
          const issue =
            error instanceof UploadError
              ? error
              : new UploadError("transfer_unavailable")
          summary.issues++
          if (issue.code === "storage_backpressure")
            summary.retryAfterMs = retryDelay(issue.retryAfterMs ?? 1000)
          this.notify(
            {
              clientIndex: row.clientIndex,
              state:
                issue.code === "storage_backpressure"
                  ? "waiting_capacity"
                  : "issue",
              receivedBytes: 0,
              errorCode: issue.code,
              retryAfterMs: issue.retryAfterMs,
            },
            signal,
          )
          if (issue.code === "permission_denied") {
            this.pause()
            throw issue
          }
        }
      }
    }
    // Drain all active promises before allowing resume; a cancelled but late PUT
    // cannot overlap the next run or persist its receipt under a new scope.
    const results = await Promise.allSettled(
      Array.from({ length: this.maxFiles }, worker),
    )
    this.check(signal)
    for (const value of results)
      if (value.status === "rejected") throw value.reason
    if (this.stopRequested) summary.stopped = true
    return summary
  }
  private async remoteParts(input: MultipartRequest, signal: AbortSignal) {
    const found = new Map<number, RemotePart>()
    const cursors = new Set<string>()
    let cursor: string | null = null
    do {
      const page = await this.call(
        () =>
          this.options.callbacks.listParts(
            { ...input, cursor, limit: 100 },
            signal,
          ),
        signal,
      )
      valid(
        sameUpload(page.identity, input.identity) &&
          Array.isArray(page.parts) &&
          page.parts.length <= 100,
      )
      for (const part of page.parts) {
        valid(
          Number.isInteger(part.partNumber) &&
            part.partNumber >= 1 &&
            part.partNumber <= input.identity.partCount &&
            !found.has(part.partNumber) &&
            typeof part.etag === "string" &&
            part.etag.trim().length > 0 &&
            part.byteSize === this.partSize(input, part.partNumber) &&
            (!part.sha256 || /^[a-f0-9]{64}$/.test(part.sha256)),
        )
        found.set(part.partNumber, {
          partNumber: part.partNumber,
          byteSize: part.byteSize,
          etag: part.etag,
          ...(part.sha256 ? { sha256: part.sha256 } : {}),
        })
      }
      cursor = page.nextCursor
      if (cursor !== null) {
        valid(
          typeof cursor === "string" &&
            cursor.length > 0 &&
            !cursors.has(cursor) &&
            page.parts.length > 0 &&
            cursors.size < 100,
        )
        cursors.add(cursor)
      }
    } while (cursor !== null)
    return found
  }
  private async recoverPermissionReceipts(
    signal: AbortSignal,
    clientIndex?: number,
  ): Promise<CancellationDrain> {
    const support = this.options.permissions
    if (!support) throw new UploadError("permission_receipts_unavailable")
    const stats: CancellationDrain = {
      completed: 0,
      unused: 0,
      unknown: 0,
      unresolved: 0,
    }
    let cursor: PermissionCursor | null = null
    let pending: PermissionRecord[] = []
    const flush = async () => {
      if (!pending.length) return
      const first = pending[0]
      const file = await this.options.store.getFile(
        this.scope,
        first.clientIndex,
      )
      valid(file?.materialId === first.identity.materialId)
      const accepted = await this.call(
        () =>
          support.acknowledgeReceipts(
            {
              scope: this.scope,
              record: file,
              identity: first.identity,
              receipts: pending.map((row) => ({
                permission: row.permission!,
                outcome: row.state as PermissionOutcome,
                etag: row.etag,
              })),
            },
            signal,
          ),
        signal,
      )
      valid(
        accepted.length === pending.length &&
          new Set(accepted).size === accepted.length &&
          pending.every((row) => accepted.includes(row.permission!.id)),
      )
      for (const row of pending)
        await support.ledger.acknowledge(row, row.permission!.id, signal)
      pending = []
    }
    do {
      this.check(signal)
      const page = await support.ledger.page(
        this.scope,
        cursor,
        100,
        clientIndex,
      )
      for (let row of page.items) {
        this.check(signal)
        if (row.state === "requested") {
          const file = await this.options.store.getFile(
            this.scope,
            row.clientIndex,
          )
          valid(file?.materialId === row.identity.materialId)
          const observation = await this.call(
            () =>
              support.readSignPermission(
                {
                  scope: this.scope,
                  record: file,
                  identity: row.identity,
                  requestId: row.requestId,
                  partNumber: row.partNumber,
                },
                signal,
              ),
            signal,
          )
          if (!observation) {
            stats.unresolved++
            continue
          }
          row = await support.ledger.signed(row, observation.permission, signal)
          // No local direct acknowledgement exists. Never manufacture one from a GET.
          if (
            observation.outcome === "unknown" ||
            observation.outcome === "completed"
          )
            row = await support.ledger.settled(
              row,
              "unknown",
              undefined,
              signal,
            )
        }
        if (row.state === "signed")
          row = await support.ledger.settled(row, "unused", undefined, signal)
        if (row.state === "armed")
          row = await support.ledger.settled(row, "unknown", undefined, signal)
        if (
          row.state !== "completed" &&
          row.state !== "unused" &&
          row.state !== "unknown"
        ) {
          stats.unresolved++
          continue
        }
        stats[row.state]++
        if (!row.acknowledged) {
          if (
            pending.length &&
            (!sameUpload(pending[0].identity, row.identity) ||
              pending[0].clientIndex !== row.clientIndex)
          )
            await flush()
          pending.push(row)
          if (pending.length === 2) await flush()
        }
      }
      cursor = page.nextCursor
    } while (cursor)
    await flush()
    return stats
  }
  private partSize(input: MultipartRequest, part: number) {
    return Math.min(
      input.identity.partSize,
      input.record.file.size - (part - 1) * input.identity.partSize,
    )
  }
  private async transfer(record: UploadFileRecord, signal: AbortSignal) {
    valid(record.materialId)
    const request = { scope: this.scope, record }
    const resumed = await this.call(
      () =>
        this.options.callbacks.resumeFile(
          { ...request, hasLocalFile: this.files.has(record.clientIndex) },
          signal,
        ),
      signal,
    )
    if (resumed.state === "waiting_capacity")
      throw new UploadError(
        "storage_backpressure",
        retryDelay(resumed.retryAfterMs),
      )
    if (resumed.state === "completed") {
      await this.options.store.bindUpload(
        this.scope,
        record.clientIndex,
        resumed.identity,
        signal,
      )
      await this.options.store.markCompleted(
        this.scope,
        record.clientIndex,
        resumed.identity,
        signal,
      )
      this.files.delete(record.clientIndex)
      this.notify(
        {
          clientIndex: record.clientIndex,
          state: "completed",
          receivedBytes: record.file.size,
        },
        signal,
      )
      return
    }
    valid(
      resumed.state === "receiving" &&
        record.file.size <= MAX_BROWSER_FILE_BYTES &&
        resumed.identity.partSize <= INITIAL_PART_BYTES,
    )
    const identity = resumed.identity
    await this.options.store.bindUpload(
      this.scope,
      record.clientIndex,
      identity,
      signal,
    )
    const file = this.files.get(record.clientIndex)
    if (!file) throw new UploadError("needs_reselect")
    if (!sameFile(record.file, fileIdentity(file)))
      throw new UploadError("wrong_file")
    const input = { ...request, identity }
    const local = new Map(
      (
        await this.options.store.listParts(
          this.scope,
          record.clientIndex,
          identity,
        )
      ).map((part) => [part.partNumber, part]),
    )
    const remote = await this.remoteParts(input, signal)
    const confirmed = new Map<number, RemotePart>()
    for (const part of remote.values()) {
      const proof = local.get(part.partNumber)
      if (
        !proof ||
        (proof.state === "confirmed" && proof.etag !== part.etag) ||
        (part.sha256 && part.sha256 !== proof.sha256)
      )
        throw new UploadError("needs_new_generation")
      this.check(signal)
      if (
        (await sha256(
          file.slice(
            (part.partNumber - 1) * identity.partSize,
            part.partNumber * identity.partSize,
          ),
        )) !== proof.sha256
      )
        throw new UploadError("wrong_file")
      await this.options.store.putPart(
        this.scope,
        record.clientIndex,
        identity,
        { ...proof, state: "confirmed", etag: part.etag },
        signal,
      )
      confirmed.set(part.partNumber, part)
    }
    let receivedBytes = [...confirmed.values()].reduce(
      (sum, part) => sum + part.byteSize,
      0,
    )
    let next = 1
    let failed = false
    const partWorker = async () => {
      while (!failed && !this.stopRequested) {
        this.check(signal)
        const part = next++
        if (part > identity.partCount) return
        if (confirmed.has(part)) continue
        try {
          const receipt = await this.sendPart(input, file, part, signal)
          confirmed.set(part, receipt)
          receivedBytes += receipt.byteSize
          this.notify(
            {
              clientIndex: record.clientIndex,
              state: "transferring",
              receivedBytes,
            },
            signal,
          )
        } catch (error) {
          failed = true
          throw error
        }
      }
    }
    const results = await Promise.allSettled(
      Array.from({ length: this.maxParts }, partWorker),
    )
    this.check(signal)
    for (const value of results)
      if (value.status === "rejected") throw value.reason
    if (this.options.permissions)
      await this.recoverPermissionReceipts(signal, record.clientIndex)
    if (this.stopRequested) return
    valid(confirmed.size === identity.partCount)
    let completedIdentity: MultipartIdentity
    try {
      completedIdentity = await this.call(
        () =>
          this.options.callbacks.completeFile(
            {
              ...input,
              parts: [...confirmed.values()].sort(
                (a, b) => a.partNumber - b.partNumber,
              ),
            },
            signal,
          ),
        signal,
      )
    } catch (error) {
      this.check(signal)
      if (error instanceof UploadError && error.code === "permission_denied")
        throw error
      throw new UploadError("completion_unknown")
    }
    await this.options.store.markCompleted(
      this.scope,
      record.clientIndex,
      identity,
      signal,
      completedIdentity,
    )
    this.files.delete(record.clientIndex)
    this.notify(
      {
        clientIndex: record.clientIndex,
        state: "completed",
        receivedBytes: file.size,
      },
      signal,
    )
  }
  private async sendPart(
    input: MultipartRequest,
    file: File,
    partNumber: number,
    signal: AbortSignal,
  ): Promise<RemotePart> {
    this.check(signal)
    const blob = file.slice(
      (partNumber - 1) * input.identity.partSize,
      partNumber * input.identity.partSize,
    )
    const hash = await sha256(blob)
    const proof: LocalPart = {
      partNumber,
      byteSize: blob.size,
      sha256: hash,
      state: "prepared",
      etag: null,
    }
    const ledger = this.options.permissions?.ledger
    let intent: SignIntent | undefined
    for (let attempt = 0; attempt < 3; attempt++) {
      this.check(signal)
      if (this.stopRequested) throw new UploadError("transfer_stopped")
      if (attempt) {
        await sleep(this.retryBaseMs * 2 ** (attempt - 1), signal)
        // Fresh authoritative state after EVERY uncertain attempt. Never trust an
        // old ETag if a failed replacement has invalidated that part on R2.
        const found = (await this.remoteParts(input, signal)).get(partNumber)
        if (found) {
          if (found.sha256 && found.sha256 !== hash)
            throw new UploadError("needs_new_generation")
          await this.options.store.putPart(
            this.scope,
            input.record.clientIndex,
            input.identity,
            { ...proof, state: "confirmed", etag: found.etag },
            signal,
          )
          return found
        }
      }
      await this.options.store.putPart(
        this.scope,
        input.record.clientIndex,
        input.identity,
        proof,
        signal,
      )
      if (ledger && !intent) {
        intent = {
          scope: this.scope,
          clientIndex: input.record.clientIndex,
          identity: input.identity,
          requestId: crypto.randomUUID(),
          partNumber,
        }
        await ledger.begin(intent, signal)
      }
      if (this.stopRequested) {
        if (ledger && intent) await ledger.unusedBeforeSend(intent, signal)
        throw new UploadError("transfer_stopped")
      }
      let armed = false
      let directCompleted = false
      try {
        const signed = await this.call(
          () =>
            this.options.callbacks.signPart(
              {
                ...input,
                partNumber,
                ...(intent ? { requestId: intent.requestId } : {}),
              },
              signal,
            ),
          signal,
        )
        if (ledger && intent) {
          valid(signed.permission)
          await ledger.signed(intent, signed.permission, signal)
          if (this.stopRequested) {
            await ledger.settled(intent, "unused", undefined, signal)
            throw new UploadError("transfer_stopped")
          }
          await ledger.armed(intent, signal)
          armed = true
          if (this.stopRequested) {
            await ledger.unusedBeforeSend(intent, signal)
            armed = false
            throw new UploadError("transfer_stopped")
          }
        } else if (this.stopRequested) throw new UploadError("transfer_stopped")
        const receipt = await this.call(
          () =>
            (this.options.callbacks.putPart ?? putSignedPart)(
              { url: signed.url, blob },
              signal,
            ),
          signal,
        )
        valid(
          typeof receipt.etag === "string" && receipt.etag.trim().length > 0,
        )
        directCompleted = true
        if (ledger && intent)
          await ledger.settled(intent, "completed", receipt.etag, signal)
        await this.options.store.putPart(
          this.scope,
          input.record.clientIndex,
          input.identity,
          { ...proof, state: "confirmed", etag: receipt.etag },
          signal,
        )
        return { partNumber, byteSize: blob.size, etag: receipt.etag }
      } catch (error) {
        this.check(signal)
        if (ledger && intent && armed && !directCompleted) {
          await ledger.settled(intent, "unknown", undefined, signal)
          intent = undefined // A fresh signature never replaces an older unknown permission.
        }
        if (
          !(error instanceof UploadError) ||
          ![
            "transfer_unavailable",
            "part_unavailable",
            "part_signature_expired",
          ].includes(error.code) ||
          attempt === 2
        )
          throw error
      }
    }
    throw new UploadError("part_unavailable")
  }
}
