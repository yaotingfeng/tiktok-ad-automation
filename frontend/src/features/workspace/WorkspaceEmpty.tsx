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
import useAuth from "@/hooks/useAuth"

export function WorkspaceEmpty({
  title = "广告搭建",
  description = "批量输入剧目与目标账户，准备素材并预览广告结构。",
  tenantId,
  canConnect = false,
}: {
  title?: string
  description?: string
  tenantId?: string | null
  canConnect?: boolean
}) {
  const { user } = useAuth()
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-2">
          <h1 className="workspace-title">{title}</h1>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
        {title === "广告搭建" && (
          <Button disabled aria-describedby="connection-required">
            新建搭建
          </Button>
        )}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{tenantId ? "连接 TikTok BC" : "接入工作空间"}</CardTitle>
          <CardDescription>
            {tenantId
              ? "为当前租户连接 TikTok BC 后，即可开始广告搭建。"
              : "完成租户接入后，即可使用投放工具。"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Empty className="min-h-72">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <Building2 />
              </EmptyMedia>
              <EmptyTitle>
                {tenantId ? "尚未连接 TikTok BC" : "尚未接入租户"}
              </EmptyTitle>
              <EmptyDescription>
                {tenantId
                  ? canConnect
                    ? "请前往当前租户的账户与授权页面完成 TikTok 授权，再通过顶栏选择 BC。"
                    : "请联系租户管理员或投手完成 TikTok 授权。你可以在账户与授权页面查看连接情况。"
                  : "请联系平台管理员开通租户并分配成员权限。接入后可连接 TikTok BC、配置版权方并开始投放。"}
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              {tenantId ? (
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
      <Alert>
        <Info />
        <AlertTitle>
          {tenantId ? "开始前需要连接 BC" : "开始前需要完成接入"}
        </AlertTitle>
        <AlertDescription id="connection-required">
          {tenantId
            ? "当前租户尚未选择可用的 TikTok BC，暂时无法创建广告。连接完成后，请通过顶栏选择本次搭建使用的 BC。"
            : "当前尚无租户和 BC 上下文，暂时无法创建广告。接入完成后，这里会显示当前租户的实际业务数据。"}
        </AlertDescription>
      </Alert>
    </>
  )
}
