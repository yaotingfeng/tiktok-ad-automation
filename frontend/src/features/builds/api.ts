import {
  BuildsService,
  type DraftInputPublic,
  type Page_DraftInputPublic_,
} from "@/client"
export const buildKey = (tenantId: string, bcId: string) =>
  ["tenant", tenantId, "builds", bcId] as const
export async function loadDraftInputs(
  tenantId: string,
  draftId: string,
  signal: AbortSignal,
  progress: (count: number) => void,
) {
  const output: { drama: string[]; account: string[] } = {
    drama: [],
    account: [],
  }
  let count = 0
  for (const kind of ["drama", "account"] as const) {
    let cursor: string | null = null
    const seen = new Set<string>()
    const rows: DraftInputPublic[] = []
    do {
      const { data }: { data: Page_DraftInputPublic_ } =
        await BuildsService.inputs({
          path: { tenant_id: tenantId, draft_id: draftId },
          query: { kind, cursor, limit: 100 },
          signal,
        })
      if (signal.aborted) throw new DOMException("Aborted", "AbortError")
      rows.push(...data.items)
      count += data.items.length
      progress(count)
      cursor = data.next_cursor ?? null
      if (cursor && seen.has(cursor)) throw new Error("重复游标，停止恢复")
      if (cursor) seen.add(cursor)
    } while (cursor)
    output[kind] = rows
      .sort((a, b) => a.line_no - b.line_no)
      .map((row) => row.raw_text)
  }
  return output
}

export const mutationKey = (tenantId: string, bcId: string, draftId: string) =>
  `build-mutation:${tenantId}:${bcId}:${draftId}`
export function readPendingMutation(
  key: string,
): { requestId: string; kind: string; prepare: boolean } | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "null")
    return value && typeof value.requestId === "string" ? value : null
  } catch {
    return null
  }
}

export async function loadDraftMaterials(
  tenantId: string,
  draftId: string,
  dramaId: string,
  signal: AbortSignal,
  progress: (count: number) => void,
) {
  const all: import("@/client").DraftMaterialPublic[] = []
  let cursor: string | null = null
  const seen = new Set<string>()
  do {
    const { data }: { data: import("@/client").Page_DraftMaterialPublic_ } =
      await BuildsService.materials({
        path: { tenant_id: tenantId, draft_id: draftId, drama_id: dramaId },
        query: { cursor, limit: 100 },
        signal,
      })
    if (signal.aborted) throw new DOMException("Aborted", "AbortError")
    all.push(...data.items)
    progress(all.length)
    cursor = data.next_cursor ?? null
    if (cursor && seen.has(cursor)) throw new Error("重复游标")
    if (cursor) seen.add(cursor)
  } while (cursor)
  return all.sort((a, b) => a.group_no - b.group_no || a.position - b.position)
}
