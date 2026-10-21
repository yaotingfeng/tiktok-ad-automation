import type { MemberPublic, UserPublic } from "@/client"
import { isoDaysAgo } from "../runtime/clock.ts"
import { DEMO_TEAM, DEMO_TENANTS, DEMO_USER } from "./world.ts"

// 用户、租户、成员管理私有数据。

/** 所有用户（演示账号 + 团队成员），支持用户管理页面 */
export const demoUsers: UserPublic[] = [
  DEMO_USER,
  ...DEMO_TEAM.map((member) => ({
    id: member.id,
    username: member.username,
    full_name: member.full_name,
    is_active: true,
    is_superuser: false,
    created_at: isoDaysAgo(90),
  })),
]

/** 主租户的成员列表 */
export const demoMembers: MemberPublic[] = DEMO_TEAM.map((member) => ({
  tenant_id: DEMO_TENANTS[0].id,
  user_id: member.id,
  role: member.role as "tenant_admin" | "operator" | "viewer",
  active: true,
  username: member.username,
  full_name: member.full_name,
  user_active: true,
}))

/** 运行时创建的用户，支持新建用户流程 */
export const demoCreatedUsers: UserPublic[] = []

/** 运行时添加的成员，支持添加成员流程 */
export const demoCreatedMembers: MemberPublic[] = []
