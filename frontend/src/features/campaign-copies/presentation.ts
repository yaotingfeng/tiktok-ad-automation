const copyErrors: Record<string, string> = {
  copy_source_contract_obsolete:
    "来源读取方式已更新，请点击重新读取来源后生成预览。",
  copy_source_read_failed: "来源素材或CTA读取失败，请重新生成预览重试。",
  copy_material_unverified: "来源视频信息不完整或不可投放，请刷新来源后重试。",
  copy_cover_unverified:
    "来源广告封面未通过核验，请补齐可验证的封面素材后重试。",
  copy_source_blocked: "来源配置不完整或当前不支持复制，请查看来源行的原因。",
  copy_source_missing: "来源系列已不可用，请返回系列列表重新选择。",
  copy_source_incomplete: "来源实际设置不完整，无法无损继承。",
  copy_source_unsupported: "来源使用了当前复制通道不支持的设置。",
  copy_source_changed:
    "来源系列已变化，请返回系列列表重新选择来源并创建复制草稿。",
  copy_currency_mismatch: "目标账户与来源币种不同；当前仅支持同币种账户复制。",
  copy_revision_conflict: "复制草稿已更新，请载入最新修订。",
  copy_preview_invalid: "预览已失效或与当前修订不一致，请重新生成。",
  copy_material_invalid: "素材必须来自此来源系列的已核验素材集合。",
  copy_material_unmapped: "来源素材缺少可用的本地映射，请补齐素材后重试。",
  copy_bid_strategy_invalid: "最高价值系列不能设置目标 ROAS。",
  copy_limit_exceeded: "本批展开超过 1000 个新系列，请减少账户或份数。",
  connection_unavailable: "当前搭建连接不可用，请恢复连接后重试。",
  connection_bc_mismatch: "搭建连接不属于当前 BC，请核对连接绑定。",
  authorization_changed: "授权或 BC 绑定已更新，请重新生成预览。",
}
export function copyErrorLabel(code: string) {
  return (
    copyErrors[code] ?? `复制未完成，请核对来源和目标设置后重试（${code}）。`
  )
}
