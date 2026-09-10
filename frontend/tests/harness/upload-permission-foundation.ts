import {
  type PermissionIdentity,
  UploadPermissionLedger,
} from "../../src/features/materials/upload-permissions"
import {
  type PermissionCallbacks,
  UploadScheduler,
} from "../../src/features/materials/upload-scheduler"
import { UploadError } from "../../src/features/materials/upload-store"
import { fixture } from "./upload-foundation"

const wait = async (condition: () => boolean) => {
  const deadline = performance.now() + 5000
  while (!condition()) {
    if (performance.now() > deadline)
      throw new Error("test boundary did not advance")
    await new Promise((resolve) => setTimeout(resolve, 2))
  }
}
export async function permissionFixture(count = 1, size = 8, partSize = 8) {
  const f = await fixture(count, size, partSize)
  const database = `permission-fixture-${crypto.randomUUID()}`
  const ledger = await UploadPermissionLedger.open(database)
  const issued = new Map<string, PermissionIdentity>()
  const signatures: string[] = [],
    acknowledgements: { ids: string[]; outcomes: string[] }[] = []
  let lostSign = false,
    lostAcknowledgement = false
  let holdSign: (() => Promise<void>) | undefined
  const sign = f.callbacks.signPart
  f.callbacks.signPart = async (input, signal) => {
    if (!input.requestId) throw new Error("missing durable signing intent")
    const local = (await ledger.page(f.scope)).items.find(
      (row) => row.requestId === input.requestId,
    )
    if (!local) throw new Error("network before durable intent")
    signatures.push(input.requestId)
    let permission = issued.get(input.requestId)
    if (!permission) {
      permission = {
        id: crypto.randomUUID(),
        nonce: crypto.randomUUID(),
        revision: 0,
        partNumber: input.partNumber,
      }
      issued.set(input.requestId, permission)
    }
    const result = await sign(input, signal)
    if (holdSign) await holdSign()
    if (lostSign) {
      lostSign = false
      throw new UploadError("transfer_unavailable")
    }
    return { ...result, permission }
  }
  const support: PermissionCallbacks = {
    ledger,
    async readSignPermission(input, signal) {
      signal.throwIfAborted()
      const permission = issued.get(input.requestId)
      return permission ? { permission, outcome: "signed" } : null
    },
    async acknowledgeReceipts(input, signal) {
      signal.throwIfAborted()
      if (input.receipts.length > 2)
        throw new Error("unbounded receipt callback")
      acknowledgements.push({
        ids: input.receipts.map((row) => row.permission.id),
        outcomes: input.receipts.map((row) => row.outcome),
      })
      if (lostAcknowledgement) {
        lostAcknowledgement = false
        throw new UploadError("transfer_unavailable")
      }
      return input.receipts.map((row) => row.permission.id)
    },
  }
  const scheduler = () =>
    new UploadScheduler({
      scope: f.scope,
      store: f.store,
      callbacks: f.callbacks,
      signal: f.abort.signal,
      permissions: support,
      maxFiles: 1,
      maxParts: 2,
      retryBaseMs: 10,
    })
  return {
    ...f,
    ledger,
    database,
    issued,
    signatures,
    acknowledgements,
    support,
    scheduler,
    loseSign: () => {
      lostSign = true
    },
    loseAcknowledgement: () => {
      lostAcknowledgement = true
    },
    holdSign: (callback: () => Promise<void>) => {
      holdSign = callback
    },
  }
}

export async function drainScenario(duringSign = false, duringArm = false) {
  const f = await permissionFixture(2, 24, 8)
  let release!: () => void
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  let armedCount = 0
  if (duringArm) {
    const arm = f.ledger.armed.bind(f.ledger)
    f.ledger.armed = async (...args) => {
      const result = await arm(...args)
      armedCount++
      await held
      return result
    }
  } else if (duringSign) f.holdSign(() => held)
  else f.hold(() => held)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const running = scheduler.run()
  await wait(() =>
    duringArm
      ? armedCount === 2
      : duringSign
        ? f.signatures.length === 2
        : f.requests.filter((row) => row.kind === "put").length === 2,
  )
  let drained = false
  const drain = scheduler.drainForCancellation().then((value) => {
    drained = true
    return value
  })
  await new Promise((resolve) => setTimeout(resolve, 20))
  const waited = !drained
  release()
  const [result, outcomes] = await Promise.all([running, drain])
  const rows = (await f.ledger.page(f.scope)).items
  const value = {
    waited,
    result,
    outcomes,
    states: rows.map((row) => row.state),
    acknowledged: rows.every((row) => row.acknowledged),
    signs: f.signatures.length,
    puts: f.requests.filter((row) => row.kind === "put").length,
    completes: f.requests.filter((row) => row.kind === "complete").length,
    receiptSizes: f.acknowledgements.map((row) => row.ids.length),
  }
  f.store.close()
  f.ledger.close()
  return value
}

export async function interruptedPermissionScenario() {
  const f = await permissionFixture()
  let release!: () => void
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  f.hold(() => held)
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const running = scheduler.run().catch((error) => error.name)
  await wait(() => f.requests.some((row) => row.kind === "put"))
  scheduler.pause()
  const callsAtPause = f.requests.length
  release()
  const error = await running
  const before = (await f.ledger.page(f.scope)).items[0].state
  const acknowledgementsAtPause = f.acknowledgements.length
  const replacement = f.scheduler()
  const outcome = await replacement.drainForCancellation()
  const after = (await f.ledger.page(f.scope)).items[0]
  const value = {
    error,
    before,
    after: after.state,
    acknowledged: after.acknowledged,
    outcome,
    callbacksAfterPause: f.requests.length - callsAtPause,
    acknowledgementsAtPause,
    receipts: f.acknowledgements,
    remoteHasPart: f.remote.get(0)?.has(1),
  }
  f.store.close()
  f.ledger.close()
  return value
}

export async function permissionRetryScenario(mode: "sign" | "put" | "ack") {
  const f = await permissionFixture()
  if (mode === "sign") f.loseSign()
  if (mode === "put") f.fault("expired")
  if (mode === "ack") f.loseAcknowledgement()
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const result = await scheduler.run()
  const recovered = await scheduler.drainForCancellation()
  const rows = (await f.ledger.page(f.scope)).items
  const value = {
    result,
    recovered,
    signatures: f.signatures,
    receipts: f.acknowledgements,
    states: rows.map((row) => row.state).sort(),
    acknowledged: rows.every((row) => row.acknowledged),
    puts: f.requests.filter((row) => row.kind === "put").length,
  }
  f.store.close()
  f.ledger.close()
  return value
}

export async function lostSigningResponseRecoveryScenario() {
  const f = await permissionFixture()
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const identity = f.identity(0)
  await f.store.bindUpload(f.scope, 0, identity)
  const intent = {
    scope: f.scope,
    clientIndex: 0,
    identity,
    requestId: crypto.randomUUID(),
    partNumber: 1,
  }
  await f.ledger.begin(intent)
  f.issued.set(intent.requestId, {
    id: crypto.randomUUID(),
    nonce: crypto.randomUUID(),
    revision: 0,
    partNumber: 1,
  })
  const outcome = await scheduler.drainForCancellation()
  const row = (await f.ledger.page(f.scope)).items[0]
  const value = {
    outcome,
    state: row.state,
    acknowledged: row.acknowledged,
    signs: f.signatures.length,
    puts: f.requests.filter((row) => row.kind === "put").length,
  }
  f.store.close()
  f.ledger.close()
  return value
}

export async function boundedPermissionRecoveryScenario() {
  const f = await permissionFixture()
  const scheduler = f.scheduler()
  await scheduler.registerFiles(f.files)
  const identity = f.identity(0)
  for (let index = 0; index < 501; index++) {
    const intent = {
      scope: f.scope,
      clientIndex: 0,
      identity,
      requestId: crypto.randomUUID(),
      partNumber: 1,
    }
    await f.ledger.begin(intent)
    await f.ledger.signed(intent, {
      id: crypto.randomUUID(),
      nonce: crypto.randomUUID(),
      revision: 0,
      partNumber: 1,
    })
  }
  const widths: number[] = []
  const page = f.ledger.page.bind(f.ledger)
  f.ledger.page = async (...args) => {
    const result = await page(...args)
    widths.push(result.items.length)
    return result
  }
  const outcome = await scheduler.drainForCancellation()
  const value = {
    outcome,
    widths,
    requests: f.acknowledgements.map((row) => row.ids.length),
    distinct: new Set(f.acknowledgements.flatMap((row) => row.ids)).size,
  }
  f.store.close()
  f.ledger.close()
  return value
}
