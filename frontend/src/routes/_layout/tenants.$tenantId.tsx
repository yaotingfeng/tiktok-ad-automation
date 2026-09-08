import {
  createFileRoute,
  Outlet,
  retainSearchParams,
} from "@tanstack/react-router"
export const Route = createFileRoute("/_layout/tenants/$tenantId")({
  validateSearch: (search: Record<string, unknown>): { bc_id?: string } => ({
    bc_id:
      typeof search.bc_id === "string" && /^\d{1,32}$/.test(search.bc_id)
        ? search.bc_id
        : undefined,
  }),
  search: { middlewares: [retainSearchParams(["bc_id"])] },
  component: Outlet,
})
