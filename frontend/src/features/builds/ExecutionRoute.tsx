import type { ExecutionRoutePublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"

export function ExecutionRoute({
  route,
}: {
  route?: ExecutionRoutePublic | null
}) {
  if (!route)
    return (
      <Alert variant="destructive">
        <AlertDescription>
          历史任务缺少可核实的执行连接，请重新准备。
        </AlertDescription>
      </Alert>
    )
  return (
    <Card role="region" aria-label="执行连接" className="min-w-0">
      <CardContent className="flex min-w-0 flex-col gap-2 text-sm">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="text-muted-foreground">执行连接</span>
          <Badge variant="outline">
            {route.channel === "OFFICIAL_MCP" ? "官方 MCP" : "官方 API"}
          </Badge>
          <span className="break-all font-medium">{route.connection_name}</span>
        </div>
        <span className="break-all">BC {route.bc_id}</span>
        <p className="text-muted-foreground">
          本任务沿用准备时选择的连接。修改 BC 默认连接只影响新任务。
        </p>
        <details>
          <summary className="cursor-pointer text-muted-foreground">
            连接编号
          </summary>
          <span className="break-all">{route.connection_id}</span>
        </details>
      </CardContent>
    </Card>
  )
}
