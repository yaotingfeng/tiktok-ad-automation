import { expect, test } from "@playwright/test"

test.beforeEach(async ({ page }) => {
  await page.goto("/tests/harness/upload-transfer.html")
})

test("transfers nine files with at most four files and two parts per file", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    const harness = await import(/* @vite-ignore */ path).catch(() => null)
    return harness ? harness.concurrencyScenario() : null
  })
  expect(value).not.toBeNull()
  expect(value.result.completed).toBe(9)
  expect(value.peaks).toEqual({ maxFiles: 4, maxParts: 2 })
  expect(value.states).toEqual(Array(9).fill("completed"))
  expect(value.putCount).toBe(27)
})

test("registration persists its request key before response loss and chunks over 200", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).registrationScenario()
  })
  expect(value.firstError).toBe("transfer_unavailable")
  expect(value.requests.map((r: { length: number }) => r.length)).toEqual([
    200, 200, 200, 1,
  ])
  expect(value.requests[0].requestId).toBe(value.pendingRequest)
  expect(value.requests[1].requestId).toBe(value.pendingRequest)
})

test("backpressure stops new file admission and resume continues the queued metadata", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).backpressureScenario()
  })
  expect(value.blocked.retryAfterMs).toBe(60000)
  expect(
    value.before.filter((r: { kind: string }) => r.kind === "resume").length,
  ).toBeLessThanOrEqual(4)
  expect(
    value.before.some(
      (r: { kind: string }) => r.kind === "sign" || r.kind === "put",
    ),
  ).toBe(false)
  expect(value.resumed.completed).toBe(9)
})

for (const mode of ["expired", "lostPut", "put", "lostComplete"] as const) {
  test(`uncertain storage result ${mode} is reconciled before bounded retries`, async ({
    page,
  }) => {
    const value = await page.evaluate(async (mode) => {
      const path = "/tests/harness/upload-foundation.ts"
      return (await import(/* @vite-ignore */ path)).retryScenario(mode)
    }, mode)
    const kinds = value.first.map((r: { kind: string }) => r.kind)
    expect(kinds.filter((kind: string) => kind === "put").length).toBe(
      mode === "put" ? 3 : mode === "expired" ? 2 : 1,
    )
    if (mode === "expired")
      expect(kinds.slice(kinds.indexOf("put") + 1, -1)).toContain("list")
    if (mode === "put") {
      expect(value.result.issues).toBe(1)
      expect(kinds).not.toContain("complete")
    } else expect(value.row.state).toBe("completed")
    if (mode === "lostComplete") {
      expect(value.result.completed).toBe(0)
      expect(value.recovered.completed).toBe(1)
      expect(
        value.requests.filter((r: { kind: string }) => r.kind === "complete"),
      ).toHaveLength(1)
      expect(
        value.requests.filter((r: { kind: string }) => r.kind === "put"),
      ).toHaveLength(1)
    }
  })
}

for (const pause of [false, true]) {
  test(`scope cancellation fences late receipts; resumable pause=${pause}`, async ({
    page,
  }) => {
    const value = await page.evaluate(async (pause) => {
      const path = "/tests/harness/upload-foundation.ts"
      return (await import(/* @vite-ignore */ path)).cancelScenario(pause)
    }, pause)
    expect(value.outcome).toBe("AbortError")
    expect(value.afterCancel).toBe(value.atCancel)
    expect(value.eventsAfterCancel).toBe(value.eventsAtCancel)
    expect(
      value.parts.every((p: { etag: string | null }) => p.etag === null),
    ).toBe(true)
    if (pause) expect(value.resumed.completed).toBe(8)
    else expect(value.finalCount).toBe(value.atCancel)
  })
}

for (const missingProof of [false, true]) {
  test(`refresh reselect uses SHA256 not ETag; missingProof=${missingProof}`, async ({
    page,
  }) => {
    const value = await page.evaluate(async (missingProof) => {
      const path = "/tests/harness/upload-foundation.ts"
      return (await import(/* @vite-ignore */ path)).reselectScenario(
        missingProof,
      )
    }, missingProof)
    expect(value.errors).toEqual(["wrong_file", "ambiguous_file"])
    if (missingProof) {
      expect(value.result.completed).toBe(0)
      expect(value.later.some((r: { kind: string }) => r.kind === "put")).toBe(
        false,
      )
      expect(value.events.at(-1).errorCode).toBe("needs_new_generation")
    } else {
      expect(value.result.completed).toBe(1)
      expect(
        value.later.filter((r: { kind: string }) => r.kind === "put"),
      ).toHaveLength(2)
    }
  })
}

test("20,000 file metadata stays chunked and seek pages stay scoped", async ({
  page,
}) => {
  test.setTimeout(60_000)
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).metadataScaleScenario()
  })
  console.log(`20k metadata registration: ${value.elapsedMs.toFixed(0)}ms`)
  expect(value.requests).toBe(100)
  expect(value.maximumChunk).toBe(200)
  expect(value.first).toBe(9990)
  expect(value.last).toBe(10089)
  expect(value.finalMaterial).toBe("material-19999")
  expect(value.foreign).toBe(0)
})

test("an old completed response cannot complete a newer object generation", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).lateCompletionScenario()
  })
  expect(value.result.completed).toBe(0)
  expect(value.row.upload.generation).toBe(2)
  expect(value.row.state).toBe("transferring")
  expect(value.events.at(-1).errorCode).toBe("upload_identity_changed")
})

test("default PUT sends exact bytes without app credentials, cookies, or referrer", async ({
  page,
}) => {
  let received: Buffer = Buffer.alloc(0)
  let headers: Record<string, string> = {}
  await page.context().addCookies([
    {
      name: "session",
      value: "must-not-send",
      domain: "storage.invalid",
      path: "/",
      secure: true,
      sameSite: "None",
    },
  ])
  await page.route("https://storage.invalid/**", async (route) => {
    headers = await route.request().allHeaders()
    received = route.request().postDataBuffer() ?? Buffer.alloc(0)
    await route.fulfill({
      status: 200,
      headers: {
        ETag: '"real-boundary-etag"',
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "ETag",
      },
    })
  })
  const value = await page.evaluate(async () => {
    localStorage.setItem("access_token", "must-not-send")
    const path = "/src/features/materials/upload-scheduler.ts"
    return (await import(/* @vite-ignore */ path)).putSignedPart(
      {
        url: "https://storage.invalid/object?signature=ephemeral",
        blob: new Blob([new Uint8Array([1, 2, 3, 4])]),
      },
      new AbortController().signal,
    )
  })
  expect(value.etag).toBe('"real-boundary-etag"')
  expect([...received]).toEqual([1, 2, 3, 4])
  expect(headers.authorization).toBeUndefined()
  expect(headers.cookie).toBeUndefined()
  expect(headers.referer).toBeUndefined()
})

test("bulk reselect handles ambiguous matches without scanning the selection for every row", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    const f = await (await import(/* @vite-ignore */ path)).fixture(3, 1, 1)
    const first = f.scheduler()
    await first.registerFiles(f.files)
    first.dispose()
    const fresh = f.scheduler()
    if (!fresh.reselectFiles) return null
    const selected = await fresh.reselectFiles([...f.files, f.files[0]])
    const result = await fresh.run()
    f.store.close()
    return { selected, result, events: f.events }
  })
  expect(value).not.toBeNull()
  if (!value) throw new Error("Bulk reselect is unavailable")
  expect(value.selected).toEqual({ matched: 2, issues: 1 })
  expect(value.result.completed).toBe(2)
  expect(
    value.events.some(
      (event: { errorCode?: string }) => event.errorCode === "ambiguous_file",
    ),
  ).toBe(true)
})

for (const corrupt of [false, true]) {
  test(`ListParts reads beyond page one and checks every page identity; corrupt=${corrupt}`, async ({
    page,
  }) => {
    const value = await page.evaluate(async (corrupt) => {
      const path = "/tests/harness/upload-foundation.ts"
      return (await import(/* @vite-ignore */ path)).paginationScenario(corrupt)
    }, corrupt)
    expect(
      value.requests.filter((r: { kind: string }) => r.kind === "list"),
    ).toHaveLength(2)
    expect(value.requests.some((r: { kind: string }) => r.kind === "put")).toBe(
      false,
    )
    expect(value.result.completed).toBe(corrupt ? 0 : 1)
    if (corrupt) expect(value.events.at(-1).errorCode).toBe("response_invalid")
  })
}

test("a failed replacement cannot reuse an ETag absent from fresh ListParts", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).invalidatedPartScenario()
  })
  expect(value.result.completed).toBe(0)
  expect(
    value.requests.filter((r: { kind: string }) => r.kind === "put"),
  ).toHaveLength(3)
  expect(
    value.requests.filter((r: { kind: string }) => r.kind === "list"),
  ).toHaveLength(3)
  expect(
    value.requests.some((r: { kind: string }) => r.kind === "complete"),
  ).toBe(false)
})

test("IndexedDB persists only scoped metadata and fences old multipart receipts", async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const modulePath = "/src/features/materials/upload-store.ts"
    const module = await import(/* @vite-ignore */ modulePath).catch(() => null)
    if (!module) return { available: false }
    const { UploadStore } = module
    const store = await UploadStore.open(`review-${crypto.randomUUID()}`)
    const scope = {
      tenantId: "tenant-a",
      bcId: "12345678901234567890",
      sessionId: "session-a",
    }
    const input = {
      ...scope,
      clientIndex: 0,
      registrationRequestId: "request-a",
      file: {
        name: "The Bond.mp4",
        size: 8,
        lastModified: 123,
        type: "video/mp4",
      },
      materialId: null,
      upload: null,
      state: "selected",
      url: "https://storage.invalid/?secret=forbidden",
      password: "must-not-persist",
      blob: new Blob(["secret"]),
    }
    await store.putFiles([input])
    await store.bindMaterials(scope, [
      { clientIndex: 0, materialId: "material-a" },
    ])
    const upload = {
      materialId: "material-a",
      generation: 1,
      uploadId: "multipart-a",
      operationRevision: 3,
      partSize: 4,
      partCount: 2,
    }
    await store.bindUpload(scope, 0, upload)
    await store.putPart(scope, 0, upload, {
      partNumber: 1,
      byteSize: 4,
      sha256: "a".repeat(64),
      etag: "receipt",
      state: "confirmed",
      url: "secret",
    })
    await store.bindUpload(scope, 0, {
      ...upload,
      generation: 2,
      uploadId: "multipart-b",
      operationRevision: 4,
    })
    let fenced = ""
    try {
      await store.putPart(scope, 0, upload, {
        partNumber: 2,
        byteSize: 4,
        sha256: "b".repeat(64),
        etag: "late",
        state: "confirmed",
      })
    } catch (error) {
      fenced = (error as Error).message
    }
    const foreign = await store.getFile({ ...scope, tenantId: "tenant-b" }, 0)
    const row = await store.getFile(scope, 0)
    const database = await new Promise<IDBDatabase>((resolve) => {
      const r = indexedDB.open(store.name)
      r.onsuccess = () => resolve(r.result)
    })
    const values: unknown[] = []
    for (const name of database.objectStoreNames) {
      values.push(
        await new Promise((resolve) => {
          const r = database.transaction(name).objectStore(name).getAll()
          r.onsuccess = () => resolve(r.result)
        }),
      )
    }
    database.close()
    store.close()
    return {
      available: true,
      fenced,
      foreign: foreign ?? null,
      row,
      durable: JSON.stringify(values),
    }
  })
  expect(result.available).toBe(true)
  expect(result.fenced).toBe("upload_identity_changed")
  expect(result.foreign).toBeNull()
  expect(result.row.upload.generation).toBe(2)
  expect(result.durable).not.toMatch(/secret|forbidden|password|"blob"|"url"/)
})

for (const stale of [false, true]) {
  test(`exclusive completion persists returned revision with original ownership fence stale=${stale}`, async ({
    page,
  }) => {
    const value = await page.evaluate(async (stale) => {
      const path = "/tests/harness/upload-foundation.ts"
      return (await import(/* @vite-ignore */ path)).completionRevisionScenario(
        stale,
      )
    }, stale)
    expect(value.row.upload.operationRevision).toBe(stale ? 12 : 9)
    expect(value.result.completed).toBe(stale ? 0 : 1)
    expect(value.row.state === "completed").toBe(!stale)
  })
}

test("short non-final ListParts page follows its cursor before completion", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).shortPartsScenario()
  })
  expect(value.resumed.completed).toBe(1)
  expect(
    value.requests.filter((r: { kind: string }) => r.kind === "put"),
  ).toHaveLength(0)
})

test("parent import intent survives refresh with immutable request scope and no secret or blob fields", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).importIntentScenario()
  })
  expect(value.saved).toEqual(value.restored)
  expect(value.found).toEqual(value.saved)
  expect(value.saved.metadataReady).toBe(true)
  expect(value.saved.serverSessionId).toBe("server-session")
  expect(value.saved).not.toHaveProperty("blob")
  expect(value.saved).not.toHaveProperty("signedUrl")
  expect(value.other).toBeNull()
  expect(value.page).toHaveLength(1)
  expect(value.changed).toBe("registration_conflict")
})

test("one reselected File cannot silently satisfy two indistinguishable manifest entries", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (
      await import(/* @vite-ignore */ path)
    ).duplicateManifestReselectScenario()
  })
  expect(value.reselect).toEqual({ matched: 0, issues: 2 })
  expect(value.result.completed).toBe(0)
  expect(
    value.requests.filter((r: { kind: string }) => r.kind === "put"),
  ).toHaveLength(0)
})

test("new generation without a multipart ID still rejects old completion receipts", async ({
  page,
}) => {
  const value = await page.evaluate(async () => {
    const path = "/tests/harness/upload-foundation.ts"
    return (await import(/* @vite-ignore */ path)).newGenerationFloorScenario()
  })
  expect(value.oldReceipt).toBe("upload_identity_changed")
  expect(value.row.minimumGeneration).toBe(2)
  expect(value.row.state).toBe("registered")
  expect(value.row.upload).toBeNull()
})

test("permission ledger persists exact intent, direct receipts and sticky unknown without URL or Blob", async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const path = "/src/features/materials/upload-permissions.ts"
    const { UploadPermissionLedger } = await import(/* @vite-ignore */ path)
    const name = `permissions-${crypto.randomUUID()}`
    let ledger = await UploadPermissionLedger.open(name)
    const scope = {
      tenantId: "tenant",
      bcId: "12345678901234567890",
      sessionId: crypto.randomUUID(),
    }
    const intent = {
      scope,
      clientIndex: 0,
      identity: {
        materialId: "material",
        generation: 1,
        uploadId: "upload",
        operationRevision: 7,
        partSize: 8,
        partCount: 1,
      },
      requestId: crypto.randomUUID(),
      partNumber: 1,
    }
    await ledger.begin({
      ...intent,
      url: "https://private.test/signed?token=secret",
      blob: new Blob(["private"]),
    })
    const permission = {
      id: crypto.randomUUID(),
      nonce: crypto.randomUUID(),
      revision: 0,
      partNumber: 1,
    }
    await ledger.signed(intent, permission)
    await ledger.armed(intent)
    await ledger.settled(intent, "unknown")
    await ledger.settled(intent, "completed", "etag-late")
    ledger.close()
    ledger = await UploadPermissionLedger.open(name)
    const page = await ledger.page(scope)
    const foreign = await ledger.page({ ...scope, tenantId: "other" })
    const raw = JSON.stringify(page)
    ledger.close()
    return {
      state: page.items[0].state,
      foreign: foreign.items.length,
      leaked:
        raw.includes("secret") || raw.includes("blob") || raw.includes("url"),
      count: page.items.length,
    }
  })
  expect(result).toEqual({
    state: "unknown",
    foreign: 0,
    leaked: false,
    count: 1,
  })
})

for (const duringSign of [false, true])
  test(`ordinary cancellation drains without abort, signed-only=${duringSign}`, async ({
    page,
  }) => {
    const result = await page.evaluate(async (duringSign) => {
      const path = "/tests/harness/upload-permission-foundation.ts"
      return (await import(/* @vite-ignore */ path)).drainScenario(duringSign)
    }, duringSign)
    expect(result.waited).toBe(true)
    expect(result.result.stopped).toBe(true)
    expect(result.signs).toBe(2)
    expect(result.puts).toBe(duringSign ? 0 : 2)
    expect(result.completes).toBe(0)
    expect(result.states).toEqual(
      Array(2).fill(duringSign ? "unused" : "completed"),
    )
    expect(result.acknowledged).toBe(true)
    expect(result.receiptSizes.every((size: number) => size <= 2)).toBe(true)
  })

test("forced pause keeps an armed PUT unknown even when ListParts later contains its ETag", async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const path = "/tests/harness/upload-permission-foundation.ts"
    return (
      await import(/* @vite-ignore */ path)
    ).interruptedPermissionScenario()
  })
  expect(result.error).toBe("AbortError")
  expect(result.before).toBe("armed")
  expect(result.after).toBe("unknown")
  expect(result.remoteHasPart).toBe(true)
  expect(result.callbacksAfterPause).toBe(0)
  expect(result.acknowledgementsAtPause).toBe(0)
  expect(result.outcome.unknown).toBe(1)
  expect(result.receipts[0].outcomes).toEqual(["unknown"])
})

for (const mode of ["sign", "put", "ack"] as const)
  test(`permission recovery preserves original facts after lost ${mode}`, async ({
    page,
  }) => {
    const result = await page.evaluate(async (mode) => {
      const path = "/tests/harness/upload-permission-foundation.ts"
      return (await import(/* @vite-ignore */ path)).permissionRetryScenario(
        mode,
      )
    }, mode)
    expect(result.acknowledged).toBe(true)
    if (mode === "sign") {
      expect(result.signatures).toHaveLength(2)
      expect(new Set(result.signatures).size).toBe(1)
      expect(result.states).toEqual(["completed"])
    } else if (mode === "put") {
      expect(new Set(result.signatures).size).toBe(2)
      expect(result.states).toEqual(["completed", "unknown"])
    } else {
      expect(result.puts).toBe(1)
      expect(result.receipts).toHaveLength(2)
      expect(result.receipts[0].ids).toEqual(result.receipts[1].ids)
    }
  })

test("lost signing response is recovered by original request GET and reported unused without PUT", async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const path = "/tests/harness/upload-permission-foundation.ts"
    return (
      await import(/* @vite-ignore */ path)
    ).lostSigningResponseRecoveryScenario()
  })
  expect(result.state).toBe("unused")
  expect(result.acknowledged).toBe(true)
  expect(result.signs).toBe(0)
  expect(result.puts).toBe(0)
  expect(result.outcome).toEqual({
    completed: 0,
    unused: 1,
    unknown: 0,
    unresolved: 0,
  })
})

test("drain while the durable arm transaction settles sends no new PUT", async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const path = "/tests/harness/upload-permission-foundation.ts"
    return (await import(/* @vite-ignore */ path)).drainScenario(false, true)
  })
  expect(result.waited).toBe(true)
  expect(result.puts).toBe(0)
  expect(result.states).toEqual(["unused", "unused"])
  expect(result.acknowledged).toBe(true)
})

test("501 permission receipts recover with 100-row local pages and at most two server receipts per callback", async ({
  page,
}) => {
  const result = await page.evaluate(async () => {
    const path = "/tests/harness/upload-permission-foundation.ts"
    return (
      await import(/* @vite-ignore */ path)
    ).boundedPermissionRecoveryScenario()
  })
  expect(result.outcome).toEqual({
    completed: 0,
    unused: 501,
    unknown: 0,
    unresolved: 0,
  })
  expect(result.widths).toEqual([100, 100, 100, 100, 100, 1])
  expect(result.requests.every((size: number) => size <= 2)).toBe(true)
  expect(result.distinct).toBe(501)
})
