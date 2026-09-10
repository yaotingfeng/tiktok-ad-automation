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

const NotFound = () => (
  <AuthLayout>
    <Card data-testid="not-found">
      <CardHeader>
        <CardTitle>
          <h1 className="workspace-title">页面不存在</h1>
        </CardTitle>
        <CardDescription>
          404 · 请检查页面地址，或返回首页继续操作。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button asChild>
          <Link to="/">返回首页</Link>
        </Button>
      </CardContent>
    </Card>
  </AuthLayout>
)

export default NotFound
