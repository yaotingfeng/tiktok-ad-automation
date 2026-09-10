import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { MaterialsPage } from "@/features/materials/MaterialsPage"
export const Route = createFileRoute("/_layout/tenants/$tenantId/materials")({
  validateSearch: z.object({
    tab: z.enum(["library", "uploads"]).optional().catch(undefined),
    batch_id: z.uuid().optional().catch(undefined),
  }),
  component: MaterialsPage,
  head: () => ({ meta: [{ title: "素材库 · TK-ADA" }] }),
})
