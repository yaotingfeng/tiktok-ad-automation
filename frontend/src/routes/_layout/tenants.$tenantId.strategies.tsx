import { createFileRoute, Outlet } from "@tanstack/react-router"
export const Route = createFileRoute("/_layout/tenants/$tenantId/strategies")({
  component: Outlet,
  head: () => ({ meta: [{ title: "投放策略 · TT ADA" }] }),
})
