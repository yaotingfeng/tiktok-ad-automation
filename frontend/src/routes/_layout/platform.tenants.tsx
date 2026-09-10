import { createFileRoute } from "@tanstack/react-router"
import { TenantAdminPage } from "@/features/tenants/TenantAdminPage"
export const Route = createFileRoute("/_layout/platform/tenants")({
  component: TenantAdminPage,
  head: () => ({ meta: [{ title: "平台租户管理 · TK-ADA" }] }),
})
