import { createFileRoute, Navigate } from "@tanstack/react-router"
export const Route = createFileRoute("/_layout/tenants/$tenantId/")({
  component: TenantHome,
})
function TenantHome() {
  const { tenantId } = Route.useParams()
  return <Navigate to="/tenants/$tenantId/builds/new" params={{ tenantId }} />
}
