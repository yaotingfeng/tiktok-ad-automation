import { Link } from "@tanstack/react-router"
import { ArrowRight, Building2, Info } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import useAuth from "@/hooks/useAuth"

export function WorkspaceEmpty({
  title = "广告搭建",
  description = "批量输入剧目与目标账户，准备素材并预览广告结构。",
  tenantId,
  canConnect = false,
  reason = "bc-required",
  resourceBcId,
}: {
  title?: string
  description?: string
  tenantId?: string | null
  canConnect?: boolean
  reason?: "bc-required" | "bc-mismatch"
  resourceBcId?: string
}) {
  const { user } = useAuth()
  const mismatch = !!tenantId && reason === "bc-mismatch"
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-2">
          <WorkspacePageTitle>{title}</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
        {title === "广告搭建" && (
          <Button disabled aria-describedby="connection-required">
            新建搭建
          </Button>
        )}
      </div>
      <div className="grid min-w-0 items-start gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(18rem,1fr)]">
        <Card>
          <CardHeader>
            <CardTitle>
              {mismatch
                ? "切换 BC 上下文"
                : tenantId
                  ? "连接 TikTok BC"
                  : "接入工作空间"}
            </CardTitle>
            <CardDescription>
              {mismatch
                ? "资源保留在原租户与 BC，不会因切换上下文而改变。"
                : tenantId
                  ? "为当前租户连接 TikTok BC 后，即可开始广告搭建。"
                  : "完成租户接入后，即可使用投放工具。"}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Empty className="items-start border-0 p-0 text-left md:p-0">
              <EmptyHeader className="max-w-xl items-start text-left">
                <EmptyMedia variant="icon">
                  <Building2 />
                </EmptyMedia>
                <EmptyTitle>
                  {mismatch
                    ? "请选择资源所属 BC"
                    : tenantId
                      ? "尚未连接 TikTok BC"
                      : "尚未接入租户"}
                </EmptyTitle>
                <EmptyDescription>
                  {mismatch
                    ? "当前选择的 BC 与资源所属范围不一致，请切换后继续查看。"
                    : tenantId
                      ? canConnect
                        ? "请前往当前租户的账户与授权页面完成 TikTok 授权，再通过顶栏选择 BC。"
                        : "请联系租户管理员完成 TikTok 授权。你可以在账户与授权页面查看连接情况。"
                      : "请联系平台管理员开通租户并分配成员权限。接入后可连接 TikTok BC、配置版权方并开始投放。"}
                </EmptyDescription>
              </EmptyHeader>
              <EmptyContent className="items-start">
                {mismatch ? (
                  resourceBcId ? (
                    <Button asChild>
                      <Link
                        to="."
                        search={(previous) => ({
                          ...previous,
                          bc_id: resourceBcId,
                        })}
                      >
                        切换到资源所属 BC
                        <ArrowRight data-icon="inline-end" />
                      </Link>
                    </Button>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      请通过顶栏选择资源所属 BC。
                    </p>
                  )
                ) : tenantId ? (
                  <Button asChild>
                    <Link
                      to="/tenants/$tenantId/accounts"
                      params={{ tenantId }}
                      search={{ tab: "connections" }}
                    >
                      查看账户与授权
                      <ArrowRight data-icon="inline-end" />
                    </Link>
                  </Button>
                ) : user?.is_superuser ? (
                  <Button asChild>
                    <Link to="/platform/tenants">
                      进入平台管理
                      <ArrowRight data-icon="inline-end" />
                    </Link>
                  </Button>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    请联系平台管理员完成接入
                  </p>
                )}
              </EmptyContent>
            </Empty>
          </CardContent>
        </Card>
        {tenantId && (
          <Card>
            <CardHeader>
              <CardTitle>无需连接 BC 即可使用</CardTitle>
              <CardDescription>
                投放策略与版权方连接属于当前租户。可用操作由你的租户角色决定。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-3">
              <Button asChild variant="outline">
                <Link to="/tenants/$tenantId/strategies" params={{ tenantId }}>
                  投放策略
                </Link>
              </Button>
              <Button asChild variant="outline">
                <Link
                  to="/tenants/$tenantId/providers"
                  params={{ tenantId }}
                  search={{ tab: "connections" }}
                >
                  版权方连接
                </Link>
              </Button>
              {canConnect && (
                <Button asChild variant="outline">
                  <Link to="/tenants/$tenantId/members" params={{ tenantId }}>
                    成员管理
                  </Link>
                </Button>
              )}
            </CardContent>
          </Card>
        )}
      </div>
      <Alert>
        <Info />
        <AlertTitle>
          {mismatch
            ? "资源按 BC 隔离"
            : tenantId
              ? "开始前需要连接 BC"
              : "开始前需要完成接入"}
        </AlertTitle>
        <AlertDescription id="connection-required">
          {mismatch
            ? "切换 BC 仅改变查看范围，不会移动资源或改变已经冻结的搭建配置。"
            : tenantId
              ? "当前租户尚未选择可用的 TikTok BC，暂时无法创建广告。连接完成后，请通过顶栏选择本次搭建使用的 BC。"
              : "当前尚无租户和 BC 上下文，暂时无法创建广告。接入完成后，这里会显示当前租户的实际业务数据。"}
        </AlertDescription>
      </Alert>
    </>
  )
}
