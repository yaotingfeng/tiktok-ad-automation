import {
  type TransferCallbacks,
  UploadScheduler,
} from "../../src/features/materials/upload-scheduler"
import {
  type MultipartIdentity,
  UploadError,
  type UploadScope,
  UploadStore,
} from "../../src/features/materials/upload-store"

export async function fixture(count = 9, size = 24, partSize = 8) {
  const scope: UploadScope = {
    tenantId: "tenant-a",
    bcId: "12345678901234567890",
    sessionId: crypto.randomUUID(),
  }
  const store = await UploadStore.open(`fixture-${crypto.randomUUID()}`)
  const files = Array.from(
    { length: count },
    (_, n) =>
      new File([new Uint8Array(size).fill(n + 1)], `The Bond ${n}.mp4`, {
        type: "video/mp4",
        lastModified: 123,
      }),
  )
  const remote = new Map<
    number,
    Map<number, { partNumber: number; byteSize: number; etag: string }>
  >()
  const completed = new Set<number>()
  const requests: {
    kind: string
    index?: number
    part?: number
    requestId?: string
    length?: number
  }[] = []
  const active = new Map<number, number>()
  let maxFiles = 0,
    maxParts = 0
  let failRegistration = false,
    backpressure = false,
    expired = false,
    failPut = false
  let lostPut = false,
    lostComplete = false
  let holdPut: (() => Promise<void>) | undefined
  const abort = new AbortController()
  const identity = (index: number): MultipartIdentity => ({
    materialId: `material-${index}`,
    generation: 1,
    uploadId: `upload-${index}`,
    operationRevision: 7,
    partSize,
    partCount: Math.ceil(size / partSize),
  })
  const check = (value: UploadScope, signal: AbortSignal) => {
    if (
      value.tenantId !== scope.tenantId ||
      value.bcId !== scope.bcId ||
      value.sessionId !== scope.sessionId ||
      signal.aborted
    )
      throw new Error("wrong boundary")
  }
  const callbacks: TransferCallbacks = {
    async registerChunk(input, signal) {
      check(input.scope, signal)
      requests.push({
        kind: "register",
        requestId: input.requestId,
        length: input.files.length,
      })
      if (failRegistration) {
        failRegistration = false
        throw new Error("response lost")
      }
      return input.files.map((file) => ({
        clientIndex: file.clientIndex,
        materialId: `material-${file.clientIndex}`,
      }))
    },
    async resumeFile(input, signal) {
      check(input.scope, signal)
      requests.push({ kind: "resume", index: input.record.clientIndex })
      if (backpressure)
        return { state: "waiting_capacity", retryAfterMs: 60000 }
      if (completed.has(input.record.clientIndex))
        return {
          state: "completed",
          identity: identity(input.record.clientIndex),
        }
      return {
        state: "receiving",
        identity: identity(input.record.clientIndex),
      }
    },
    async listParts(input, signal) {
      check(input.scope, signal)
      requests.push({ kind: "list", index: input.record.clientIndex })
      const offset = Number(input.cursor ?? 0)
      const parts = [
        ...(remote.get(input.record.clientIndex)?.values() ?? []),
      ].sort((a, b) => a.partNumber - b.partNumber)
      return {
        identity: identity(input.record.clientIndex),
        parts: parts.slice(offset, offset + input.limit),
        nextCursor:
          parts.length > offset + input.limit
            ? String(offset + input.limit)
            : null,
      }
    },
    async signPart(input, signal) {
      check(input.scope, signal)
      requests.push({
        kind: "sign",
        index: input.record.clientIndex,
        part: input.partNumber,
      })
      return {
        url: `https://storage.invalid/${input.record.clientIndex}/${input.partNumber}?signature=NEVER-PERSIST`,
      }
    },
    async putPart(input, signal) {
      signal.throwIfAborted()
      const url = new URL(input.url),
        [index, part] = url.pathname.slice(1).split("/").map(Number)
      requests.push({ kind: "put", index, part })
      active.set(index, (active.get(index) ?? 0) + 1)
      maxFiles = Math.max(maxFiles, active.size)
      maxParts = Math.max(maxParts, active.get(index)!)
      try {
        if (holdPut) await holdPut()
        else await new Promise((resolve) => setTimeout(resolve, 15))
        if (expired) {
          expired = false
          throw new UploadError("part_signature_expired")
        }
        if (failPut) throw new UploadError("part_unavailable")
        const rows = remote.get(index) ?? new Map()
        rows.set(part, {
          partNumber: part,
          byteSize: input.blob.size,
          etag: `etag-${index}-${part}`,
        })
        remote.set(index, rows)
        if (lostPut) {
          lostPut = false
          throw new UploadError("part_unavailable")
        }
        return { etag: `etag-${index}-${part}` }
      } finally {
        const left = active.get(index)! - 1
        if (left) active.set(index, left)
        else active.delete(index)
      }
    },
    async completeFile(input, signal) {
      check(input.scope, signal)
      requests.push({ kind: "complete", index: input.record.clientIndex })
      if (input.parts.length !== identity(input.record.clientIndex).partCount)
        throw new Error("incomplete parts")
      completed.add(input.record.clientIndex)
      if (lostComplete) {
        lostComplete = false
        throw new Error("lost completion")
      }
      return input.identity
    },
  }
  const events: unknown[] = []
  const scheduler = () =>
    new UploadScheduler({
      scope,
      store,
      callbacks,
      signal: abort.signal,
      retryBaseMs: 10,
      onProgress: (event) => events.push(event),
    })
  return {
    scope,
    store,
    files,
    remote,
    completed,
    requests,
    events,
    abort,
    callbacks,
    identity,
    scheduler,
    peaks: () => ({ maxFiles, maxParts }),
    fault: (
      name:
        | "registration"
        | "backpressure"
        | "expired"
        | "put"
        | "lostPut"
        | "lostComplete",
      value = true,
    ) => {
      if (name === "registration") failRegistration = value
      if (name === "backpressure") backpressure = value
      if (name === "expired") expired = value
      if (name === "put") failPut = value
      if (name === "lostPut") lostPut = value
      if (name === "lostComplete") lostComplete = value
    },
    hold: (fn?: () => Promise<void>) => {
      holdPut = fn
    },
  }
}

export async function concurrencyScenario() {
  const f = await fixture()
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const result = await scheduler.run()
  const rows = await f.store.listFiles(f.scope)
  f.store.close()
  return {
    result,
    peaks: f.peaks(),
    states: rows.map((row) => row.state),
    putCount: f.requests.filter((x) => x.kind === "put").length,
  }
}

export async function registrationScenario() {
  const f = await fixture(401, 1, 1)
  f.fault("registration")
  let firstError = ""
  try {
    await f.scheduler().registerFiles(f.files)
  } catch (error) {
    firstError = (error as Error).message
  }
  const pending = await f.store.listFiles(f.scope)
  await f.scheduler().registerFiles(f.files)
  const requests = f.requests.filter((r) => r.kind === "register")
  f.store.close()
  return {
    firstError,
    pendingRequest: pending[0].registrationRequestId,
    requests,
  }
}

export async function backpressureScenario() {
  const f = await fixture()
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  f.fault("backpressure")
  const blocked = await scheduler.run()
  const before = f.requests.slice()
  f.fault("backpressure", false)
  const resumed = await scheduler.resume()
  f.store.close()
  return { blocked, before, resumed }
}

export async function retryScenario(
  mode: "expired" | "lostPut" | "put" | "lostComplete",
) {
  const f = await fixture(1, 8, 8)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  f.fault(mode)
  const result = await scheduler.run()
  const first = f.requests.slice()
  let recovered = null
  if (mode === "lostComplete") recovered = await f.scheduler().run() // No File references after refresh.
  const row = await f.store.getFile(f.scope, 0)
  f.store.close()
  return {
    result,
    first,
    requests: f.requests,
    row,
    recovered,
    events: f.events,
  }
}

export async function cancelScenario(pause: boolean) {
  const f = await fixture(8)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  let release!: () => void
  let entered!: () => void
  const waiting = new Promise<void>((resolve) => {
    release = resolve
  })
  const entry = new Promise<void>((resolve) => {
    entered = resolve
  })
  f.hold(async () => {
    entered()
    await waiting
  })
  const running = scheduler.run().catch((error: Error) => error.name)
  await entry
  if (pause) scheduler.pause()
  else f.abort.abort()
  const atCancel = f.requests.length
  const eventsAtCancel = f.events.length
  release()
  const outcome = await running
  const parts = await f.store.listParts(f.scope, 0, f.identity(0))
  const afterCancel = f.requests.length
  const eventsAfterCancel = f.events.length
  let resumed = null
  f.hold()
  if (pause) resumed = await scheduler.resume()
  else {
    try {
      await scheduler.registerFiles(f.files)
    } catch {
      /* Permanently aborted scope. */
    }
    try {
      await scheduler.run()
    } catch {
      /* Permanently aborted scope. */
    }
  }
  const finalCount = f.requests.length
  f.store.close()
  return {
    outcome,
    atCancel,
    afterCancel,
    finalCount,
    eventsAtCancel,
    eventsAfterCancel,
    parts,
    resumed,
  }
}

export async function reselectScenario(missingProof: boolean) {
  const f = await fixture(1)
  const originalPut = f.callbacks.putPart!
  f.callbacks.putPart = async (input, signal) => {
    if (!new URL(input.url).pathname.endsWith("/1"))
      throw new UploadError("part_unavailable")
    return originalPut(input, signal)
  }
  const first = f.scheduler()
  await first.registerFiles(f.files)
  await first.run()
  first.dispose()
  const scheduler = f.scheduler()
  const wrong = new File([new Uint8Array(24).fill(9)], f.files[0].name, {
    type: "video/mp4",
    lastModified: 123,
  })
  const errors: string[] = []
  for (const candidates of [[wrong], [f.files[0], f.files[0]]]) {
    try {
      await scheduler.reselect(0, candidates)
    } catch (error) {
      errors.push((error as Error).message)
    }
  }
  if (missingProof) {
    const db = await new Promise<IDBDatabase>((resolve) => {
      const r = indexedDB.open(f.store.name)
      r.onsuccess = () => resolve(r.result)
    })
    await new Promise<void>((resolve) => {
      const tx = db.transaction("parts", "readwrite")
      tx.objectStore("parts").clear()
      tx.oncomplete = () => resolve()
    })
    db.close()
  }
  await scheduler.reselect(0, f.files)
  f.callbacks.putPart = originalPut
  const before = f.requests.length
  const result = await scheduler.run()
  const later = f.requests.slice(before)
  const row = await f.store.getFile(f.scope, 0)
  f.store.close()
  return { errors, result, later, row, events: f.events }
}

export async function metadataScaleScenario() {
  const f = await fixture(20_000, 1, 1)
  const started = performance.now()
  await f.scheduler().registerFiles(f.files)
  const elapsedMs = performance.now() - started
  const page = await f.store.listFiles(f.scope, 9989, 100)
  const foreign = await f.store.listFiles({ ...f.scope, tenantId: "tenant-b" })
  const last = await f.store.getFile(f.scope, 19_999)
  f.store.close()
  return {
    elapsedMs,
    requests: f.requests.length,
    maximumChunk: Math.max(...f.requests.map((r) => r.length ?? 0)),
    first: page[0].clientIndex,
    last: page[99].clientIndex,
    finalMaterial: last?.materialId,
    foreign: foreign.length,
  }
}

export async function lateCompletionScenario() {
  const f = await fixture(1, 8, 8)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  f.callbacks.resumeFile = async () => {
    await f.store.bindUpload(f.scope, 0, {
      ...f.identity(0),
      generation: 2,
      uploadId: "new-upload",
      operationRevision: 9,
    })
    return { state: "completed", identity: f.identity(0) }
  }
  const result = await scheduler.run()
  const row = await f.store.getFile(f.scope, 0)
  f.store.close()
  return { result, row, events: f.events }
}

export async function paginationScenario(corrupt: boolean) {
  const f = await fixture(1, 101, 1)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const upload = f.identity(0)
  await f.store.bindUpload(f.scope, 0, upload)
  const hash = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array([1]))),
    (n) => n.toString(16).padStart(2, "0"),
  ).join("")
  const parts = new Map<
    number,
    { partNumber: number; byteSize: number; etag: string }
  >()
  for (let partNumber = 1; partNumber <= 101; partNumber++) {
    await f.store.putPart(f.scope, 0, upload, {
      partNumber,
      byteSize: 1,
      sha256: hash,
      etag: `etag-${partNumber}`,
      state: "confirmed",
    })
    parts.set(partNumber, {
      partNumber,
      byteSize: 1,
      etag: `etag-${partNumber}`,
    })
  }
  f.remote.set(0, parts)
  if (corrupt) {
    const list = f.callbacks.listParts
    f.callbacks.listParts = async (input, signal) => {
      const page = await list(input, signal)
      if (input.cursor)
        return { ...page, identity: { ...page.identity, operationRevision: 8 } }
      return page
    }
  }
  const result = await scheduler.run()
  f.store.close()
  return { result, requests: f.requests, events: f.events }
}

export async function invalidatedPartScenario() {
  const f = await fixture(1, 8, 8)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const identity = f.identity(0)
  await f.store.bindUpload(f.scope, 0, identity)
  const hash = Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", await f.files[0].arrayBuffer()),
    ),
    (n) => n.toString(16).padStart(2, "0"),
  ).join("")
  // A previous replacement failed: the application must honor R2's empty fresh
  // ListParts, even though the browser still has the old successful receipt.
  await f.store.putPart(f.scope, 0, identity, {
    partNumber: 1,
    byteSize: 8,
    sha256: hash,
    state: "confirmed",
    etag: "old-invalidated-etag",
  })
  f.fault("put")
  const result = await scheduler.run()
  f.store.close()
  return { result, requests: f.requests }
}

export async function completionRevisionScenario(stale = false) {
  const f = await fixture(1, 8, 8)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  f.callbacks.completeFile = async () => {
    if (stale)
      await f.store.bindUpload(f.scope, 0, {
        ...f.identity(0),
        operationRevision: 12,
      })
    return { ...f.identity(0), operationRevision: 9 }
  }
  const result = await scheduler.run()
  const row = await f.store.getFile(f.scope, 0)
  f.store.close()
  return { result, row }
}

export async function shortPartsScenario() {
  const f = await fixture(1, 2, 1)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  await f.store.bindUpload(f.scope, 0, f.identity(0))
  const hash = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array([1]))),
    (n) => n.toString(16).padStart(2, "0"),
  ).join("")
  for (let partNumber = 1; partNumber <= 2; partNumber++)
    await f.store.putPart(f.scope, 0, f.identity(0), {
      partNumber,
      byteSize: 1,
      sha256: hash,
      etag: `part-${partNumber}`,
      state: "confirmed",
    })
  f.callbacks.listParts = async (input) => ({
    identity: input.identity,
    parts: [
      {
        partNumber: input.cursor ? 2 : 1,
        byteSize: 1,
        etag: input.cursor ? "part-2" : "part-1",
      },
    ],
    nextCursor: input.cursor ? null : "opaque-next",
  })
  const resumed = await scheduler.run()
  f.store.close()
  return { resumed, requests: f.requests }
}

export async function importIntentScenario() {
  const store = await UploadStore.open(`imports-${crypto.randomUUID()}`)
  const scope = {
    tenantId: "tenant-a",
    bcId: "123",
    sessionId: crypto.randomUUID(),
  }
  const intent = {
    ...scope,
    requestId: scope.sessionId,
    fileCount: 20000,
    totalBytes: 100000,
    serverSessionId: null,
    metadataReady: false,
    createdAt: Date.now(),
    blob: new Blob(["never-store"]),
    signedUrl: "https://private.invalid/?secret=never-store",
  }
  await store.putImport(intent)
  await store.putImport({
    ...intent,
    metadataReady: true,
    serverSessionId: "server-session",
  })
  let changed = ""
  try {
    await store.putImport({ ...intent, totalBytes: 1 })
  } catch (error) {
    changed = (error as Error).message
  }
  const saved = await store.getImport(scope)
  const found = await store.findImport(
    scope.tenantId,
    scope.bcId,
    "server-session",
  )
  const other = await store.findImport("tenant-b", scope.bcId, "server-session")
  const page = await store.listImports(scope.tenantId, scope.bcId)
  const name = store.name
  store.close()
  const reopened = await UploadStore.open(name)
  const restored = await reopened.getImport(scope)
  reopened.close()
  return { saved, found, other: other ?? null, page, changed, restored }
}

export async function duplicateManifestReselectScenario() {
  const f = await fixture(2, 8, 8)
  f.files[1] = new File([new Uint8Array(8).fill(2)], f.files[0].name, {
    type: f.files[0].type,
    lastModified: f.files[0].lastModified,
  })
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  scheduler.dispose()
  const replacement = f.scheduler()
  const reselect = await replacement.reselectFiles([f.files[0]])
  const result = await replacement.run()
  f.store.close()
  return { reselect, result, requests: f.requests, events: f.events }
}

export async function newGenerationFloorScenario() {
  const f = await fixture(1, 8, 8)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  await scheduler.run()
  await f.store.resetGeneration(f.scope, 0, 2)
  let oldReceipt = ""
  try {
    await f.store.bindUpload(f.scope, 0, f.identity(0))
  } catch (error) {
    oldReceipt = (error as Error).message
  }
  const row = await f.store.getFile(f.scope, 0)
  f.store.close()
  return { oldReceipt, row }
}
