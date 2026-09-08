import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { BuildPreparationPage } from "@/features/builds/BuildPreparationPage"
export const Route = createFileRoute(
  "/_layout/tenants/$tenantId/build-drafts/$draftId",
)({
  validateSearch: z.object({
    prepare: z.boolean().optional().catch(undefined),
    edit: z.boolean().optional().catch(undefined),
  }),
  component: BuildPreparationPage,
})
