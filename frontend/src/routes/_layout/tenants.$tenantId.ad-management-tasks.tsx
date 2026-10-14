import { createFileRoute, Outlet } from "@tanstack/react-router"

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/ad-management-tasks",
)({
  component: Outlet,
  head: () => ({ meta: [{ title: "操作记录 · TK-ADA" }] }),
})
