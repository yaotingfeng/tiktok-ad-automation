import { Building2 } from "lucide-react"
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
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { useTenantScope } from "./TenantScope"

export function TenantPlaceholder({ title }: { title: string }) {
  const { tenant, scope } = useTenantScope()
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-2">
          <WorkspacePageTitle>{title}</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            {tenant!.name} · 当前租户工作空间
          </p>
        </div>
        {title === "广告搭建" && scope?.role !== "viewer" && (
          <Button disabled aria-describedby="bc-required">
            新建搭建
          </Button>
        )}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{scope?.bcId ? "功能准备中" : "接入 TikTok BC"}</CardTitle>
          <CardDescription>在当前租户中连接业务资产后继续。</CardDescription>
        </CardHeader>
        <CardContent>
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <Building2 />
              </EmptyMedia>
              <EmptyTitle>
                {scope?.bcId ? "当前功能尚未开放" : "尚未连接 TikTok BC"}
              </EmptyTitle>
              <EmptyDescription id="bc-required">
                {scope?.bcId
                  ? "已选择当前 BC；此业务功能将在后续版本接入。"
                  : "请联系管理员完成 TikTok 接入。当前没有可用的 BC，暂时无法创建广告或读取账户素材。"}
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        </CardContent>
      </Card>
    </>
  )
}
