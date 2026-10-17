/** 部分素材缺数与整个账户报表过期分别说明，避免把无法判断解释为普通零命中。 */
export const MATERIAL_ISSUES: Record<string, string> = {
  MATERIAL_REPORT_UNLINKED: "报表有尚未定位的帖子，单广告表现完整性无法确认",
  MATERIAL_LINK_MISSING: "素材与报表尚未关联",
  MATERIAL_LINK_UNRESOLVED: "历史帖子尚未准确关联到广告内素材",
  MATERIAL_SHARED_REPORT: "多条广告共用报表无法拆分",
  MATERIAL_IDENTITY_AMBIGUOUS: "当前广告内素材身份缺失或不唯一",
  MATERIAL_METRIC_MISSING: "判断所需的报表指标不完整",
}

export function inputIssueText(item: Record<string, string>) {
  if (item.reason === "STALE_REPORT")
    return `账户 ${item.advertiser_id}：报表数据未更新，已跳过`
  return `账户 ${item.advertiser_id}：${item.count ?? "部分"} 个广告内素材无法判断，${MATERIAL_ISSUES[item.reason] ?? "素材数据不完整"}`
}
