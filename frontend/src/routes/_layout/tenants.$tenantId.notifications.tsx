import { createFileRoute } from "@tanstack/react-router"
import { NotificationSettingsPage } from "@/features/automation-rules/NotificationSettingsPage"

export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/notifications",
)({
  component: NotificationSettingsPage,
  head: () => ({ meta: [{ title: "通知设置 · TK-ADA" }] }),
})
