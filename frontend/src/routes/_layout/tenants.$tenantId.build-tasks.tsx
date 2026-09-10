import { createFileRoute, Outlet } from "@tanstack/react-router"
export const Route = createFileRoute("/_layout/tenants/$tenantId/build-tasks")({
  component: Outlet,
  head: () => ({ meta: [{ title: "搭建任务 · TK-ADA" }] }),
})
