import { createFileRoute } from "@tanstack/react-router"
import { StrategyList } from "@/features/strategies/StrategyList"
export const Route = createFileRoute("/_layout/tenants/$tenantId/strategies/")({
  component: StrategyList,
})
