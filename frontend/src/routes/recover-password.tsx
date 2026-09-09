import { createFileRoute, Link } from "@tanstack/react-router"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
export const Route = createFileRoute("/recover-password")({
  component: RecoverPassword,
})
function RecoverPassword() {
  return (
    <AuthLayout>
      <Card>
        <CardHeader>
          <CardTitle>联系管理员重置密码</CardTitle>
          <CardDescription>
            请联系平台管理员核实账号身份并设置新密码。本系统不提供邮件找回。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild>
            <Link to="/login">返回登录</Link>
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}
