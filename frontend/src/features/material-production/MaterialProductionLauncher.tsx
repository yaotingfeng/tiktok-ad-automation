import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useEffect } from "react"
import { TenantsService } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { errorMessage } from "@/features/tenants/shared"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { useMaterialProduction } from "./useMaterialProduction"

// 按租户名称直达素材制作工作台：体验版的“素材制作”入口会新开本页（例如 ?tenant=junbo）。
// 本页走生产版的正常登录与权限：只有已登录且对该租户有素材权限的账号才能免密进入，
// 体验版本身不持有任何凭证。名称优先按不区分大小写的完整匹配；没有完整匹配时，
// 只有唯一一个包含该名称的租户才使用，避免误入其他租户。
export function MaterialProductionLauncher({
  tenantName,
}: {
  tenantName: string
}) {
  const tenant = useQuery({
    queryKey: ["material-production-launcher", tenantName],
    enabled: !!tenantName,
    queryFn: async ({ signal }) => {
      const { data } = await TenantsService.getMyTenants({
        query: { search: tenantName, active: true, limit: 50 },
        signal,
      })
      const name = tenantName.trim().toLowerCase()
      const exact = data.items.find(
        (item) => item.name.trim().toLowerCase() === name,
      )
      if (exact) return exact
      const partial = data.items.filter((item) =>
        item.name.toLowerCase().includes(name),
      )
      return partial.length === 1 ? partial[0] : null
    },
  })
  const tenantId = tenant.data?.id
  const production = useMaterialProduction(tenantId)
  const { open } = production
  // 租户确定后自动发起免密登录；hook 在租户变化时会先重置旧请求，所以放在它之后执行。
  // biome-ignore lint/correctness/useExhaustiveDependencies: 只在租户确定时触发一次。
  useEffect(() => {
    if (tenantId) open()
  }, [tenantId])

  return (
    <div className="flex max-w-xl flex-col gap-4">
      <WorkspacePageTitle>打开素材制作</WorkspacePageTitle>
      {!tenantName ? (
        <Alert variant="destructive">
          <AlertTitle>缺少租户</AlertTitle>
          <AlertDescription>链接中没有指定要打开的租户。</AlertDescription>
        </Alert>
      ) : tenant.isPending ? (
        <p role="status">正在查找租户 {tenantName}…</p>
      ) : tenant.error ? (
        <Alert variant="destructive">
          <AlertTitle>暂时无法读取租户</AlertTitle>
          <AlertDescription>{errorMessage(tenant.error)}</AlertDescription>
        </Alert>
      ) : !tenant.data ? (
        <Alert variant="destructive">
          <AlertTitle>当前账号不能打开 {tenantName} 的素材制作</AlertTitle>
          <AlertDescription>
            没有找到名为 {tenantName}{" "}
            的可用租户，或当前账号不是该租户成员。请换用有权限的账号登录。
          </AlertDescription>
        </Alert>
      ) : (
        <>
          <p role="status">
            {production.pending
              ? `${production.pendingLabel} ${tenant.data.name} 素材工作台`
              : `如果没有自动打开 ${tenant.data.name} 的素材工作台，请点下面的按钮重试。`}
          </p>
          <div className="flex gap-2">
            <Button disabled={production.pending} onClick={open}>
              打开素材工作台
            </Button>
            <Button variant="outline" asChild>
              <Link
                to="/tenants/$tenantId"
                params={{ tenantId: tenant.data.id }}
              >
                进入租户
              </Link>
            </Button>
          </div>
        </>
      )}
    </div>
  )
}
