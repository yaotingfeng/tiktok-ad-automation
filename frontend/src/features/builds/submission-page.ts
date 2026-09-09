import { useMemo, useState } from "react"
export const submissionKey = (tenant: string, bc: string) =>
  ["tenant", tenant, "submissions", bc] as const
function loadPage(key: string): { cursors: (string | null)[]; limit: number } {
  try {
    const s = JSON.parse(sessionStorage.getItem(key) || "null")
    if (
      s &&
      Array.isArray(s.cursors) &&
      s.cursors.length &&
      s.cursors.length <= 1000 &&
      s.cursors.every((c: unknown) => c === null || typeof c === "string") &&
      [50, 100].includes(s.limit)
    )
      return s
  } catch {}
  return { cursors: [null], limit: 50 }
}
export function useSubmissionPaging(key: string) {
  const initial = useMemo(() => loadPage(key), [key])
  const [saved, setSaved] = useState({ key, data: initial })
  const state = saved.key === key ? saved.data : initial
  const change = (update: (prev: typeof state) => typeof state) => {
    const data = update(state)
    try {
      sessionStorage.setItem(key, JSON.stringify(data))
    } catch {}
    setSaved({ key, data })
  }
  return {
    cursor: state.cursors[state.cursors.length - 1] ?? null,
    limit: state.limit,
    page: state.cursors.length,
    reset: () => change((s) => ({ ...s, cursors: [null] })),
    setLimit: (limit: number) => change(() => ({ limit, cursors: [null] })),
    previous: () =>
      change((s) => ({
        ...s,
        cursors: s.cursors.length > 1 ? s.cursors.slice(0, -1) : s.cursors,
      })),
    next: (cursor: string) =>
      change((s) => ({ ...s, cursors: [...s.cursors, cursor] })),
  }
}
export function localDate(value: Date) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`
}
export function dateRange(days: number) {
  const to = new Date(),
    from = new Date(to)
  from.setDate(from.getDate() - days + 1)
  return { from: localDate(from), to: localDate(to) }
}
export function isoDate(date: string, end = false) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return undefined
  const [y, m, d] = date.split("-").map(Number)
  return new Date(y, m - 1, d + (end ? 1 : 0)).toISOString()
}
export const listLocationKey = (tenant: string, bc: string) =>
  `submission-list-location:${tenant}:${bc}`
export function rememberedList(
  tenant: string,
  bc: string,
): Record<string, string> {
  try {
    const s = JSON.parse(
      sessionStorage.getItem(listLocationKey(tenant, bc)) || "{}",
    )
    return { ...s, bc_id: bc }
  } catch {
    return { bc_id: bc }
  }
}
