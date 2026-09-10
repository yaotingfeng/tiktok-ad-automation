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
  await page
    .context()
    .addCookies([
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
