import {
  defaultParseSearch,
  defaultStringifySearch,
} from "@tanstack/react-router"

// TikTok IDs exceed Number.MAX_SAFE_INTEGER. Keep the raw URL string before
// Router's default JSON parsing, while preserving its behavior for other fields.
export function parseWorkspaceSearch(search: string) {
  const parsed = defaultParseSearch(search) as Record<string, unknown>
  const bcId = new URLSearchParams(search).get("bc_id")
  if (bcId !== null) parsed.bc_id = bcId
  return parsed
}
export function stringifyWorkspaceSearch(search: Record<string, unknown>) {
  const { bc_id, ...rest } = search
  const params = new URLSearchParams(defaultStringifySearch(rest))
  if (typeof bc_id === "string") params.set("bc_id", bc_id)
  const value = params.toString()
  return value ? `?${value}` : ""
}
