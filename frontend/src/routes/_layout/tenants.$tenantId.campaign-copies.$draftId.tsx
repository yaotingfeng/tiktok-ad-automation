import { createFileRoute } from "@tanstack/react-router"
import { CopyWorkspace } from "@/features/campaign-copies/CopyWorkspace"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/campaign-copies/$draftId",
)({ component: CopyWorkspace })
