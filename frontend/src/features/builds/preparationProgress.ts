import type { DraftInputPublic, DraftSummary } from "@/client"

export function preparationLabel(summary: DraftSummary) {
  switch (summary.preparation_phase) {
    case "accounts":
      return "正在核对账户与授权…"
    case "links":
      return "正在确认剧目与推广链接…"
    case "materials":
      return "正在准备素材与推广小程序…"
    default:
      return "正在准备…"
  }
}

export function materialWaitingLabel(
  input: DraftInputPublic,
  summary: DraftSummary,
) {
  if (["empty", "duplicate", "invalid"].includes(input.status))
    return "无需匹配"
  if (summary.status === "DRAFT") return "尚未开始"
  if (summary.status === "BLOCKED") return "准备已暂停"
  if (
    summary.status === "PREPARING" &&
    summary.preparation_phase === "accounts"
  )
    return "等待账户核对完成"
  const linkState = input.preparation?.link_status || input.status
  if (linkState === "needs_resolution") return "请先选择剧目"
  if (
    [
      "failed",
      "not_found",
      "blocked_auth",
      "config_conflict",
      "result_unknown",
    ].includes(linkState)
  )
    return "请先处理推广链接"
  if (linkState !== "ready") return "等待推广链接确认"
  return "等待匹配"
}
