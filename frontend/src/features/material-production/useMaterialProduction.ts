import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { MaterialProductionService } from "@/client"
import { errorMessage } from "@/features/tenants/shared"

export function useMaterialProduction(tenantId?: string) {
  const [pending, setPending] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const currentTenant = useRef(tenantId)
  currentTenant.current = tenantId

  useEffect(() => {
    currentTenant.current = tenantId
    setPending(false)
    return () => {
      controller.current?.abort()
      controller.current = null
    }
  }, [tenantId])

  const open = async () => {
    if (!tenantId || controller.current) return
    const request = new AbortController()
    controller.current = request
    setPending(true)
    try {
      // 仅点击时签发；不通过 query/effect 预取，不缓存或打印临时链接。
      const { data } =
        await MaterialProductionService.productionPostMaterialLoginLink({
          path: { tenant_id: tenantId },
          body: {},
          signal: request.signal,
        })
      if (request.signal.aborted || currentTenant.current !== tenantId) return
      const target = new URL(data.login_url)
      if (
        target.origin !== "https://manjumt.gzjunbo.net" ||
        target.pathname !== "/api/login-links/consume" ||
        target.username ||
        target.password ||
        target.hash
      ) {
        throw new Error("Invalid material login destination")
      }
      window.location.assign(target.href)
    } catch (error) {
      if (!request.signal.aborted && currentTenant.current === tenantId) {
        toast.error(errorMessage(error))
      }
    } finally {
      if (controller.current === request) {
        controller.current = null
        setPending(false)
      }
    }
  }
  return { open: () => void open(), pending }
}
