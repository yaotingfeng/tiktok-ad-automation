import {
  type IngestFilePublic,
  type IngestSummary,
  MaterialIngestService,
  type Page_IngestFilePublic_,
} from "@/client"
import { handleApiError } from "@/lib/api-feedback"
import {
  canRestartOriginal,
  ingestCallbacks,
  transferError,
} from "./ingest-transfer"
import { type TransferProgress, UploadScheduler } from "./upload-scheduler"
import { UploadError, type UploadImport, UploadStore } from "./upload-store"

export type IngestManagerState = {
  creating: boolean
  transferring: boolean
  unfinished: boolean
  forbidden: boolean
  pending: UploadImport | null
  sessionId: string | null
  error: unknown
}
export class IngestManager {
  private store: UploadStore | null = null
  private abort = new AbortController()
  private scheduler: UploadScheduler | null = null
  private draining: Promise<unknown> | null = null
  private intent: UploadImport | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private token: string | null = null
  private opening: Promise<void> | null = null
  private listeners = new Set<() => void>()
  private progressListeners = new Map<number, Set<() => void>>()
  private progress = new Map<number, TransferProgress>()
  private snapshot: IngestManagerState = {
    creating: false,
    transferring: false,
    unfinished: false,
    forbidden: false,
    pending: null,
    sessionId: null,
    error: null,
  }
  constructor(
    readonly tenantId: string,
    readonly bcId: string,
  ) {}
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }
  getProgress = (index: number) => this.progress.get(index)
  subscribeProgress = (index: number, listener: () => void) => {
    const listeners = this.progressListeners.get(index) ?? new Set()
    listeners.add(listener)
    this.progressListeners.set(index, listeners)
    return () => {
      listeners.delete(listener)
      if (!listeners.size) this.progressListeners.delete(index)
    }
  }
  private update(next: Partial<IngestManagerState>) {
    this.snapshot = { ...this.snapshot, ...next }
    for (const listener of this.listeners) listener()
  }
  activate() {
    if (this.abort.signal.aborted) this.abort = new AbortController()
    this.token = localStorage.getItem("access_token")
    const signal = this.abort.signal
    this.opening = (async () => {
      try {
        const store = await UploadStore.open()
        if (signal.aborted) {
          store.close()
          return
        }
        this.store = store
        let after: string | null = null
        do {
          const imports = await store.listImports(
            this.tenantId,
            this.bcId,
            after,
          )
          this.guard(signal)
          const pending = imports.find((row) => !row.serverSessionId)
          if (pending) {
            this.intent = pending
            this.update({ pending, unfinished: true })
            return
          }
          if (imports.length < 100) return
          after = imports[imports.length - 1].sessionId
        } while (after)
      } catch (error) {
        if (!signal.aborted) this.update({ error })
      }
    })()
  }
  dispose() {
    this.abort.abort()
    this.scheduler?.dispose()
    this.scheduler = null
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.store?.close()
    this.store = null
    this.progress.clear()
  }
  revokePermission = () => {
    if (this.snapshot.forbidden && this.abort.signal.aborted) return
    this.dispose()
    this.update({ forbidden: true, creating: false, transferring: false })
  }
  checkAuthentication = () => {
    if (!this.token || localStorage.getItem("access_token") !== this.token)
      this.revokePermission()
  }
  guard = (signal: AbortSignal) => {
    signal.throwIfAborted()
    this.abort.signal.throwIfAborted()
    if (!this.token || localStorage.getItem("access_token") !== this.token) {
      this.revokePermission()
      throw new UploadError("permission_denied")
    }
  }
  private async ready() {
    await this.opening
    this.guard(this.abort.signal)
    if (!this.store) throw new UploadError("storage_unavailable")
    return this.store
  }
  private failure(error: unknown) {
    if (error instanceof Error && error.name === "AbortError") return
    if (error instanceof Error) handleApiError(error)
    const safe = transferError(error)
    if (safe instanceof UploadError && safe.code === "permission_denied")
      this.revokePermission()
    this.update({ error: safe })
  }
  private bind(intent: UploadImport) {
    this.scheduler?.dispose()
    this.intent = intent
    this.progress.clear()
    this.scheduler = new UploadScheduler({
      scope: intent,
      store: this.store!,
      signal: this.abort.signal,
      callbacks: ingestCallbacks({
        tenantId: this.tenantId,
        sessionId: () => {
          if (!this.intent?.serverSessionId)
            throw new UploadError("registration_required")
          return this.intent.serverSessionId
        },
        guard: this.guard,
        observe: () => {},
      }),
      onProgress: (event) => {
        this.progress.set(event.clientIndex, event)
        for (const listener of this.progressListeners.get(event.clientIndex) ??
          [])
          listener()
      },
    })
    this.update({ sessionId: intent.serverSessionId })
    return this.scheduler
  }
  private checkSummary(summary: IngestSummary, intent?: UploadImport) {
    if (
      summary.bc_id !== this.bcId ||
      (intent &&
        (summary.expected_count !== intent.fileCount ||
          summary.total_bytes !== intent.totalBytes ||
          (intent.serverSessionId &&
            summary.session_id !== intent.serverSessionId)))
    )
      throw new UploadError("response_invalid")
  }
  start = async (files: readonly File[]): Promise<IngestSummary | null> => {
    if (
      this.snapshot.creating ||
      this.snapshot.transferring ||
      this.snapshot.pending ||
      this.snapshot.forbidden
    )
      return null
    this.update({ creating: true, error: null })
    try {
      const store = await this.ready()
      const requestId = crypto.randomUUID()
      const intent: UploadImport = {
        tenantId: this.tenantId,
        bcId: this.bcId,
        sessionId: requestId,
        requestId,
        fileCount: files.length,
        totalBytes: files.reduce((sum, file) => sum + file.size, 0),
        serverSessionId: null,
        metadataReady: false,
        createdAt: Date.now(),
      }
      await store.putImport(intent, this.abort.signal)
      const scheduler = this.bind(intent)
      await scheduler.stageFiles(files)
      intent.metadataReady = true
      await store.putImport(intent, this.abort.signal)
      this.update({ pending: intent, unfinished: true })
      return await this.create(intent)
    } catch (error) {
      this.failure(error)
      return null
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
  }
  private async create(intent: UploadImport) {
    if (!intent.metadataReady) throw new UploadError("needs_reselect")
    this.guard(this.abort.signal)
    const summary = (
      await MaterialIngestService.createIngestSession({
        path: { tenant_id: this.tenantId },
        body: {
          bc_id: this.bcId,
          request_id: intent.requestId,
          file_count: intent.fileCount,
          total_bytes: intent.totalBytes,
        },
        signal: this.abort.signal,
      })
    ).data
    this.guard(this.abort.signal)
    return this.accept(summary, intent)
  }
  private async accept(summary: IngestSummary, intent: UploadImport) {
    this.checkSummary(summary, intent)
    const accepted = { ...intent, serverSessionId: summary.session_id }
    await this.store!.putImport(accepted, this.abort.signal)
    this.intent = accepted
    if (!this.scheduler) this.bind(accepted)
    this.update({ pending: null, sessionId: summary.session_id, error: null })
    void this.run()
    return summary
  }
  recover = async (): Promise<IngestSummary | null> => {
    if (this.snapshot.creating || this.snapshot.forbidden) return null
    this.update({ creating: true, error: null })
    try {
      await this.ready()
      const intent = this.snapshot.pending
      if (!intent) return null
      this.guard(this.abort.signal)
      const summary = (
        await MaterialIngestService.readIngestRequest({
          path: { tenant_id: this.tenantId, request_id: intent.requestId },
          query: { bc_id: this.bcId },
          signal: this.abort.signal,
        })
      ).data
      this.guard(this.abort.signal)
      return await this.accept(summary, intent)
    } catch (error) {
      this.failure(error)
      return null
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
  }
  restorePendingFiles = async (
    files: readonly File[],
  ): Promise<IngestSummary | null> => {
    if (
      this.snapshot.creating ||
      !this.snapshot.pending ||
      this.snapshot.forbidden
    )
      return null
    this.update({ creating: true, error: null })
    try {
      const store = await this.ready()
      const intent = this.snapshot.pending!
      if (
        files.length !== intent.fileCount ||
        files.reduce((sum, file) => sum + file.size, 0) !== intent.totalBytes
      )
        throw new UploadError("wrong_file")
      const scheduler = this.scheduler ?? this.bind(intent)
      await scheduler.stageFiles(files)
      const complete = { ...intent, metadataReady: true }
      await store.putImport(complete, this.abort.signal)
      this.intent = complete
      this.update({ pending: complete })
      return await this.create(complete)
    } catch (error) {
      this.failure(error)
      return null
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
  }
  retryCreation = async (): Promise<IngestSummary | null> => {
    if (
      this.snapshot.creating ||
      !this.snapshot.pending ||
      this.snapshot.forbidden
    )
      return null
    this.update({ creating: true, error: null })
    try {
      await this.ready()
      return await this.create(this.snapshot.pending!)
    } catch (error) {
      this.failure(error)
      return null
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
  }
  private async run() {
    if (
      this.snapshot.transferring ||
      !this.scheduler ||
      !this.intent?.serverSessionId
    )
      return
    const scheduler = this.scheduler,
      intent = this.intent,
      signal = this.abort.signal
    this.update({ transferring: true, error: null })
    try {
      const transfer = scheduler.run()
      this.draining = transfer
      const result = await transfer
      this.draining = null
      this.guard(signal)
      if (this.scheduler !== scheduler) return
      const seal = (
        await MaterialIngestService.sealIngestSession({
          path: {
            tenant_id: this.tenantId,
            session_id: intent.serverSessionId!,
          },
          signal,
        })
      ).data
      this.guard(signal)
      if (this.scheduler !== scheduler) return
      if (!seal.sealed) throw new UploadError("registration_required")
      this.update({ unfinished: result.issues > 0 })
      if (result.retryAfterMs)
        this.timer = setTimeout(() => {
          this.timer = null
          if (!signal.aborted) void this.run()
        }, result.retryAfterMs)
    } catch (error) {
      if (signal.aborted || this.scheduler !== scheduler) return
      this.failure(error)
      this.update({ unfinished: true })
    } finally {
      if (this.scheduler === scheduler) this.draining = null
      if (!signal.aborted && this.scheduler === scheduler)
        this.update({ transferring: false })
    }
  }
  observePage = async (
    sessionId: string,
    files: readonly IngestFilePublic[],
  ) => {
    if (
      !this.store ||
      !this.intent ||
      this.intent.serverSessionId !== sessionId ||
      files.length > 100 ||
      this.abort.signal.aborted
    )
      return
    const intent = this.intent
    for (const file of files) {
      const row = await this.store.getFile(intent, file.client_index)
      this.guard(this.abort.signal)
      if (
        row?.materialId === file.material_id &&
        file.generation >
          Math.max(row.minimumGeneration ?? 1, row.upload?.generation ?? 1)
      )
        await this.store.resetGeneration(
          intent,
          row.clientIndex,
          file.generation,
          this.abort.signal,
        )
    }
  }
  pause = () => {
    this.scheduler?.pause()
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
  }
  /** Merely opening history never creates or signs anything. */
  openSession = async (summary: IngestSummary) => {
    await this.ready()
    this.checkSummary(summary)
    if (this.intent?.serverSessionId === summary.session_id) return
    this.pause()
    await this.draining?.catch(() => {})
    this.guard(this.abort.signal)
    const existing = await this.store!.findImport(
      this.tenantId,
      this.bcId,
      summary.session_id,
    )
    this.guard(this.abort.signal)
    this.scheduler?.dispose()
    this.scheduler = null
    this.intent = existing ?? null
    if (existing) this.bind(existing)
    this.update({
      sessionId: summary.session_id,
      unfinished: false,
      transferring: false,
    })
  }
  private async hydrate(summary: IngestSummary) {
    const store = await this.ready()
    this.checkSummary(summary)
    let intent = await store.findImport(
      this.tenantId,
      this.bcId,
      summary.session_id,
    )
    if (!intent) {
      const key = crypto.randomUUID()
      intent = {
        tenantId: this.tenantId,
        bcId: this.bcId,
        sessionId: key,
        requestId: key,
        serverSessionId: summary.session_id,
        fileCount: summary.expected_count,
        totalBytes: summary.total_bytes,
        metadataReady: false,
        createdAt: Date.now(),
      }
      await store.putImport(intent, this.abort.signal)
    }
    if (!intent.metadataReady) {
      const cursors = new Set<string>()
      let cursor: string | null = null
      do {
        this.guard(this.abort.signal)
        const page: Page_IngestFilePublic_ = (
          await MaterialIngestService.listIngestFiles({
            path: { tenant_id: this.tenantId, session_id: summary.session_id },
            query: { cursor, limit: 100 },
            signal: this.abort.signal,
          })
        ).data
        this.guard(this.abort.signal)
        if (page.items.length)
          await store.putFiles(
            page.items.map((file) => ({
              ...intent!,
              clientIndex: file.client_index,
              registrationRequestId: `accepted:${file.material_id}`,
              file: {
                name: file.file_name,
                size: file.size,
                lastModified: file.last_modified_ms ?? 0,
                type: file.mime_type,
              },
              materialId: file.material_id,
              upload: null,
              state: "registered",
            })),
            this.abort.signal,
          )
        cursor = page.next_cursor ?? null
        if (cursor) {
          if (!page.items.length || cursors.has(cursor) || cursors.size >= 200)
            throw new UploadError("response_invalid")
          cursors.add(cursor)
        }
      } while (cursor)
      intent = { ...intent, metadataReady: true }
      await store.putImport(intent, this.abort.signal)
    }
    if (this.intent?.sessionId !== intent.sessionId || !this.scheduler)
      this.bind(intent)
    return this.scheduler!
  }
  resume = async (summary: IngestSummary, files?: readonly File[]) => {
    if (
      this.snapshot.transferring ||
      this.snapshot.creating ||
      this.snapshot.forbidden
    )
      return
    this.update({ creating: true, error: null })
    try {
      const scheduler = await this.hydrate(summary)
      if (files) await scheduler.reselectFiles(files)
    } catch (error) {
      this.failure(error)
      return
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
    await this.run()
  }
  reselectFile = async (
    summary: IngestSummary,
    index: number,
    files: readonly File[],
  ) => {
    if (
      this.snapshot.transferring ||
      this.snapshot.creating ||
      this.snapshot.forbidden
    )
      return
    this.update({ creating: true, error: null })
    try {
      const scheduler = await this.hydrate(summary)
      await scheduler.reselect(index, files)
    } catch (error) {
      this.failure(error)
      return
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
    await this.run()
  }
  cancel = async (summary: IngestSummary, file: IngestFilePublic) => {
    if (
      this.snapshot.transferring ||
      this.snapshot.creating ||
      this.snapshot.forbidden ||
      file.operation_status === "result_unknown"
    )
      return
    this.update({ creating: true, error: null })
    try {
      await this.ready()
      this.checkSummary(summary)
      this.guard(this.abort.signal)
      await MaterialIngestService.cancelIngestFile({
        path: {
          tenant_id: this.tenantId,
          session_id: summary.session_id,
          material_id: file.material_id,
        },
        body: {
          generation: file.generation,
          upload_id: file.upload_id,
          operation_revision: file.operation_revision,
        },
        signal: this.abort.signal,
      })
      this.guard(this.abort.signal)
    } catch (error) {
      this.failure(error)
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
  }
  retry = async (summary: IngestSummary, file: IngestFilePublic) => {
    if (
      !canRestartOriginal(file) ||
      this.snapshot.transferring ||
      this.snapshot.creating ||
      this.snapshot.forbidden
    )
      return
    this.update({ creating: true, error: null })
    try {
      await this.hydrate(summary)
      this.guard(this.abort.signal)
      const next = (
        await MaterialIngestService.createIngestGeneration({
          path: {
            tenant_id: this.tenantId,
            session_id: summary.session_id,
            material_id: file.material_id,
          },
          body: {
            generation: file.generation,
            upload_id: file.upload_id,
            operation_revision: file.operation_revision,
          },
          signal: this.abort.signal,
        })
      ).data
      this.guard(this.abort.signal)
      if (
        next.material_id !== file.material_id ||
        next.generation <= file.generation
      )
        throw new UploadError("response_invalid")
      await this.store!.resetGeneration(
        this.intent!,
        file.client_index,
        next.generation,
        this.abort.signal,
      )
    } catch (error) {
      this.failure(error)
      return
    } finally {
      if (!this.abort.signal.aborted) this.update({ creating: false })
    }
    await this.run()
  }
}
