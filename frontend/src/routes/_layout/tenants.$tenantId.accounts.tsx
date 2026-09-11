import { createFileRoute } from "@tanstack/react-router"
import { AccountsPage } from "@/features/accounts/AccountsPage"
export const Route = createFileRoute("/_layout/tenants/$tenantId/accounts")({
  validateSearch: (
    search: Record<string, unknown>,
  ): {
    tab: "accounts" | "connections"
    authorization?: string
    connection_id?: string
    mcp_authorization?: string
    attempt_id?: string
  } => ({
    tab: search.tab === "connections" ? "connections" : "accounts",
    authorization:
      typeof search.authorization === "string" &&
      /^[A-Za-z_]{1,80}$/.test(search.authorization)
        ? search.authorization
        : undefined,
    mcp_authorization:
      typeof search.mcp_authorization === "string" &&
      /^[A-Za-z_]{1,80}$/.test(search.mcp_authorization)
        ? search.mcp_authorization
        : undefined,
    attempt_id:
      typeof search.attempt_id === "string" &&
      /^[a-f0-9-]{36}$/i.test(search.attempt_id)
        ? search.attempt_id
        : undefined,
    connection_id:
      typeof search.connection_id === "string" &&
      /^[a-f0-9-]{36}$/i.test(search.connection_id)
        ? search.connection_id
        : undefined,
  }),
  component: AccountsPage,
  head: () => ({ meta: [{ title: "账户与授权 · TK-ADA" }] }),
})
