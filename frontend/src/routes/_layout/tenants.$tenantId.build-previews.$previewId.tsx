import { createFileRoute } from "@tanstack/react-router"
import { PreviewWorkspace } from "@/features/builds/PreviewWorkspace"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/build-previews/$previewId",
)({ component: PreviewWorkspace })
