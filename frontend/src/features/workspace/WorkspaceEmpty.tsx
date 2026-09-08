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
}: {
  title?: string
  description?: string
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
          <CardTitle>接入工作空间</CardTitle>
          <CardDescription>完成租户接入后，即可使用投放工具。</CardDescription>
        </CardHeader>
        <CardContent>
          <Empty className="min-h-72">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <Building2 />
              </EmptyMedia>
              <EmptyTitle>尚未接入租户</EmptyTitle>
              <EmptyDescription>
                请联系平台管理员开通租户并分配成员权限。接入后可连接 TikTok
                BC、配置版权方并开始投放。
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              {user?.is_superuser ? (
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
        <AlertTitle>开始前需要完成接入</AlertTitle>
        <AlertDescription id="connection-required">
          当前尚无租户和 BC
          上下文，暂时无法创建广告。接入完成后，这里会显示当前租户的实际业务数据。
        </AlertDescription>
      </Alert>
    </>
  )
}
