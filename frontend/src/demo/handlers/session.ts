import type { TenantSummary, UserPublic } from "@/client"
import { DEMO_TENANTS, DEMO_TOKEN, DEMO_USER } from "../fixtures/world"
import { matchesSearch, paginate } from "../runtime/paging"
import { handle } from "../runtime/registry"

// 登录与当前用户：体验版任何用户名密码都能登录，统一进入演示账号。
export const demoSession = {
  user: { ...DEMO_USER } as UserPublic,
}

handle("login-login_access_token", () => ({
  access_token: DEMO_TOKEN,
  token_type: "bearer",
}))

handle("login-test_token", () => demoSession.user)

handle("users-read_user_me", () => demoSession.user)

handle("utils-health_check", () => true)

/** 租户列表在平台管理里可被新增和修改，这里保存可变副本。 */
export const demoTenants: TenantSummary[] = DEMO_TENANTS.map((tenant) => ({
  ...tenant,
}))

handle("tenants-get_my_tenants", ({ query }) =>
  paginate(
    demoTenants.filter(
      (tenant) =>
        matchesSearch(query.search, tenant.name, tenant.id) &&
        (query.active == null || tenant.active === query.active),
    ),
    query,
  ),
)
