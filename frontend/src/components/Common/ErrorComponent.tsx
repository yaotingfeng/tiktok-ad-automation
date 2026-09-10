import { Link } from "@tanstack/react-router"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AuthLayout } from "./AuthLayout"

const ErrorComponent = () => (
  <AuthLayout>
    <Card data-testid="error-component">
      <CardHeader>
        <CardTitle>
          <h1 className="workspace-title">页面暂时无法加载</h1>
        </CardTitle>
        <CardDescription>请稍后重试，或返回首页重新进入。</CardDescription>
      </CardHeader>
      <CardContent>
        <Button asChild>
          <Link to="/">返回首页</Link>
        </Button>
      </CardContent>
    </Card>
  </AuthLayout>
)

export default ErrorComponent
