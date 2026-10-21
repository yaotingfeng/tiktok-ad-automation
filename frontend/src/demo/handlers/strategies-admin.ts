import type {
  MemberPublic,
  StrategyConfig_Input,
  TenantSummary,
  UserPublic,
  VersionPublic,
} from "@/client"
import {
  demoCreatedMembers,
  demoCreatedUsers,
  demoMembers,
  demoUsers,
} from "../fixtures/admin"
import {
  demoCopyPool,
  demoCreatedStrategies,
  demoStrategyRecords,
} from "../fixtures/strategies"
import { demoUuid, nextId } from "../runtime/clock"
import { matchesSearch, paginate } from "../runtime/paging"
import { DemoReply, demoError, handle } from "../runtime/registry"
import { demoSession, demoTenants } from "./session.ts"

// 体验版假后端：strategies-admin 领域（投放策略 + 用户/租户/平台管理）。

// ==================== 投放策略 ====================

handle("strategies-get_strategies", ({ query }) => {
  const allStrategies = [
    ...demoCreatedStrategies.map((s) => s.record),
    ...demoStrategyRecords.map((s) => s.record),
  ]
  const filtered = allStrategies.filter((s) => {
    if (query.active != null && s.active !== query.active) return false
    if (query.query && !matchesSearch(query.query, s.name)) return false
    return true
  })
  return paginate(filtered, query)
})

handle("strategies-get_one", ({ path }) => {
  const strategy = [...demoStrategyRecords, ...demoCreatedStrategies].find(
    (s) => s.record.id === path.strategy_id,
  )
  if (!strategy) {
    return demoError(404, "strategy_not_found", "策略不存在")
  }
  return strategy.record
})

handle("strategies-versions", ({ path }) => {
  const strategy = [...demoStrategyRecords, ...demoCreatedStrategies].find(
    (s) => s.record.id === path.strategy_id,
  )
  if (!strategy) {
    return demoError(404, "strategy_not_found", "策略不存在")
  }
  // 返回版本列表，按版本号倒序
  const items = [...strategy.versions].reverse()
  return {
    items,
    next_cursor: null,
    total: items.length,
  }
})

handle("strategies-version", ({ path }) => {
  const allVersions = [
    ...demoStrategyRecords.flatMap((s) => s.versions),
    ...demoCreatedStrategies.flatMap((s) => s.versions),
  ]
  const version = allVersions.find((v) => v.id === path.version_id)
  if (!version) {
    return demoError(404, "version_not_found", "版本不存在")
  }
  return version
})

handle("strategies-copy_pool", () => {
  // 所有策略都使用同一个文案池
  return demoCopyPool
})

handle("strategies-validate", () => {
  // 体验版：所有配置都通过校验
  return {
    valid: true,
    errors: [],
    scene_check_pending: false,
  }
})

// 策略保存：创建与追加版本都同步返回新版本（与接口约定一致）；按 request_id 记录结果，
// 网络中断后前端用 saved_request 回查时返回同一个版本，不会重复保存。
const savedVersions = new Map<string, VersionPublic>()

function saveVersion(input: {
  request_id: string
  strategy_id: string | null
  name?: string | null
  config: StrategyConfig_Input
}) {
  const existing = savedVersions.get(input.request_id)
  if (existing) return existing
  const records = [...demoStrategyRecords, ...demoCreatedStrategies]
  const strategy = input.strategy_id
    ? records.find((s) => s.record.id === input.strategy_id)
    : null
  if (input.strategy_id && !strategy) return null
  const strategyId = strategy?.record.id ?? demoUuid(nextId())
  const version: VersionPublic = {
    id: demoUuid(nextId()),
    strategy_id: strategyId,
    number: strategy ? strategy.record.latest_version + 1 : 1,
    config: normalizeConfig(input.config),
    created_by: demoSession.user.id,
    created_at: new Date().toISOString(),
    request_id: input.request_id,
  }
  if (strategy) {
    strategy.versions.push(version)
    strategy.record.latest_version = version.number
    strategy.record.version_id = version.id
    strategy.record.config = version.config
    if (input.name) strategy.record.name = input.name
  } else {
    // 新策略排在列表最前，方便演示时看到刚创建的策略。
    demoCreatedStrategies.unshift({
      record: {
        id: strategyId,
        name: input.name || "新策略",
        active: true,
        latest_version: 1,
        version_id: version.id,
        config: version.config,
        created_by: demoSession.user.id,
        created_at: version.created_at,
      },
      versions: [version],
    })
  }
  savedVersions.set(input.request_id, version)
  return version
}

handle("strategies-create", ({ body }) => {
  const version = saveVersion({ ...body, strategy_id: null })
  return new DemoReply(201, version)
})

handle("strategies-append", ({ path, body }) => {
  const version = saveVersion({ ...body, strategy_id: path.strategy_id })
  return version
    ? new DemoReply(201, version)
    : demoError(404, "strategy_not_found", "策略不存在")
})

handle("strategies-saved_request", ({ path }) => {
  const version = savedVersions.get(path.request_id)
  return version ?? demoError(404, "request_not_found", "保存请求不存在")
})

handle("strategies-set_state", ({ path, body }) => {
  const strategy = [...demoStrategyRecords, ...demoCreatedStrategies].find(
    (s) => s.record.id === path.strategy_id,
  )
  if (!strategy) {
    return demoError(404, "strategy_not_found", "策略不存在")
  }
  if (body.active != null) {
    strategy.record.active = body.active
  }
  if (body.name) {
    strategy.record.name = body.name
  }
  return strategy.record
})

// 辅助函数：规范化配置（Input → Output）
function normalizeConfig(input: StrategyConfig_Input): any {
  return {
    ...input,
    budget: String(input.budget),
    target_roas: input.target_roas != null ? String(input.target_roas) : null,
    budget_strategy: input.budget_strategy || "SERIES",
    bid_strategy: input.bid_strategy || "HIGHEST_VALUE",
    creation_status: input.creation_status || "ENABLE",
    schedule_type: input.schedule_type || "SCHEDULE_FROM_NOW",
    group_generation_mode: input.group_generation_mode || "BY_MATERIAL",
    ad_generation_mode: input.ad_generation_mode || "FIXED",
    creative_count: input.creative_count || 1,
    campaign_name_template:
      input.campaign_name_template || "{provider_drama}-{drama_id}",
    targeting: input.targeting || {
      region_mode: "ALL_AVAILABLE",
      region_codes: [],
      languages: [],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    },
  }
}

// ==================== 用户管理 ====================

handle("users-read_users", ({ query }) => {
  const allUsers = [...demoUsers, ...demoCreatedUsers]
  const result = paginate(allUsers, query, (u) => u.id)
  return {
    data: result.items,
    count: result.total,
  }
})

handle("users-read_user_by_id", ({ path }) => {
  const user = [...demoUsers, ...demoCreatedUsers].find(
    (u) => u.id === path.user_id,
  )
  if (!user) {
    return demoError(404, "user_not_found", "用户不存在")
  }
  return user
})

handle("users-create_user", ({ body }) => {
  const newUser: UserPublic = {
    id: demoUuid(nextId()),
    username: body.username,
    full_name: body.full_name || null,
    is_active: body.is_active ?? true,
    is_superuser: body.is_superuser ?? false,
    created_at: new Date().toISOString(),
  }
  demoCreatedUsers.push(newUser)
  return newUser
})

handle("users-update_user", ({ path, body }) => {
  const user = [...demoUsers, ...demoCreatedUsers].find(
    (u) => u.id === path.user_id,
  )
  if (!user) {
    return demoError(404, "user_not_found", "用户不存在")
  }
  if (body.full_name !== undefined) user.full_name = body.full_name
  if (body.is_active !== undefined) user.is_active = body.is_active ?? true
  if (body.is_superuser !== undefined)
    user.is_superuser = body.is_superuser ?? false
  return user
})

handle("users-delete_user", ({ path }) => {
  const index = demoCreatedUsers.findIndex((u) => u.id === path.user_id)
  if (index >= 0) {
    demoCreatedUsers.splice(index, 1)
  }
  return { message: "用户已删除" }
})

handle("users-update_user_me", ({ body }) => {
  // 修改当前用户信息（运行时修改 session.ts 导出的对象）
  if (body.full_name !== undefined) {
    demoSession.user.full_name = body.full_name
  }
  return demoSession.user
})

handle("users-update_password_me", () => {
  // 体验版：密码修改总是成功
  return { message: "密码修改成功" }
})

handle("users-delete_user_me", () => {
  // 体验版：不真正删除，只返回成功消息
  return { message: "账号删除成功" }
})

// ==================== 租户管理 ====================

handle("tenants-get_platform_tenants", ({ query }) => {
  const filtered = demoTenants.filter((t) => {
    if (query.search && !matchesSearch(query.search, t.name)) return false
    if (query.active != null && t.active !== query.active) return false
    return true
  })
  return paginate(filtered, query, (t) => t.id)
})

handle("tenants-patch_tenant", ({ path, body }) => {
  const tenant = demoTenants.find((t) => t.id === path.tenant_id)
  if (!tenant) {
    return demoError(404, "tenant_not_found", "租户不存在")
  }
  if (body.name) tenant.name = body.name
  if (body.active != null) tenant.active = body.active
  if (body.default_bc_id !== undefined)
    tenant.default_bc_id = body.default_bc_id
  return tenant
})

handle("tenants-post_tenant", ({ body }) => {
  // 新建租户同时创建用户
  const newUser: UserPublic = {
    id: demoUuid(nextId()),
    username: body.username,
    full_name: null,
    is_active: true,
    is_superuser: false,
    created_at: new Date().toISOString(),
  }
  demoCreatedUsers.push(newUser)

  const newTenant: TenantSummary = {
    id: demoUuid(nextId()),
    name: body.name,
    active: true,
    role: "tenant_admin",
    default_bc_id: null,
  }
  demoTenants.push(newTenant)

  return newTenant
})

handle("tenants-get_platform_user_candidates", ({ query }) => {
  // 返回所有用户作为候选
  const allUsers = [...demoUsers, ...demoCreatedUsers]
  const filtered = allUsers.filter((u) => {
    if (
      query.query &&
      !matchesSearch(query.query, u.username, u.full_name || "")
    )
      return false
    return true
  })
  const result = paginate(filtered, query, (u) => u.id)
  // 转换为 UserCandidate 格式
  return {
    items: result.items.map((u) => ({
      id: u.id,
      username: u.username,
      full_name: u.full_name || null,
    })),
    next_cursor: result.next_cursor,
    total: result.total,
  }
})

// ==================== 成员管理 ====================

handle("tenants-get_members", ({ path, query }) => {
  const allMembers = [...demoMembers, ...demoCreatedMembers].filter(
    (m) => m.tenant_id === path.tenant_id,
  )
  const filtered = allMembers.filter((m) => {
    if (
      query.search &&
      !matchesSearch(query.search, m.username, m.full_name || "")
    )
      return false
    if (query.active != null && m.active !== query.active) return false
    return true
  })
  return paginate(filtered, query)
})

handle("tenants-get_member_candidates", ({ path, query }) => {
  // 返回还不是该租户成员的用户
  const existingMembers = [...demoMembers, ...demoCreatedMembers]
    .filter((m) => m.tenant_id === path.tenant_id)
    .map((m) => m.user_id)
  const allUsers = [...demoUsers, ...demoCreatedUsers]
  const candidates = allUsers.filter((u) => !existingMembers.includes(u.id))
  const filtered = candidates.filter((u) => {
    if (
      query.query &&
      !matchesSearch(query.query, u.username, u.full_name || "")
    )
      return false
    return true
  })
  const result = paginate(filtered, query, (u) => u.id)
  // 转换为 UserCandidate 格式
  return {
    items: result.items.map((u) => ({
      id: u.id,
      username: u.username,
      full_name: u.full_name || null,
    })),
    next_cursor: result.next_cursor,
    total: result.total,
  }
})

handle("tenants-put_member", ({ path, body }) => {
  const member = [...demoMembers, ...demoCreatedMembers].find(
    (m) => m.tenant_id === path.tenant_id && m.user_id === body.user_id,
  )
  if (member) {
    // 更新现有成员
    member.role = body.role
    member.active = body.active
    return member
  }
  // 新增成员
  const user = [...demoUsers, ...demoCreatedUsers].find(
    (u) => u.id === body.user_id,
  )
  if (!user) {
    return demoError(404, "user_not_found", "用户不存在")
  }
  const newMember: MemberPublic = {
    tenant_id: path.tenant_id,
    user_id: body.user_id,
    role: body.role,
    active: body.active ?? true,
    username: user.username,
    full_name: user.full_name || null,
    user_active: user.is_active ?? true,
  }
  demoCreatedMembers.push(newMember)
  return newMember
})

handle("tenants-post_member_user", ({ path, body }) => {
  // 新建用户并添加为成员
  const newUser: UserPublic = {
    id: demoUuid(nextId()),
    username: body.username,
    full_name: body.full_name || null,
    is_active: true,
    is_superuser: false,
    created_at: new Date().toISOString(),
  }
  demoCreatedUsers.push(newUser)

  const newMember: MemberPublic = {
    tenant_id: path.tenant_id,
    user_id: newUser.id,
    role: body.role,
    active: true,
    username: newUser.username,
    full_name: newUser.full_name || null,
    user_active: true,
  }
  demoCreatedMembers.push(newMember)

  return newMember
})
