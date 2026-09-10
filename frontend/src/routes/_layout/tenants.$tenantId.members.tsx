import { createFileRoute } from "@tanstack/react-router"
import { MembersPage } from "@/features/tenants/MembersPage"
export const Route = createFileRoute("/_layout/tenants/$tenantId/members")({
  component: MembersPage,
  head: () => ({ meta: [{ title: "成员管理 · TK-ADA" }] }),
})
