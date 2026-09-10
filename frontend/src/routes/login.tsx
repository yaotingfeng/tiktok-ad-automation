import { zodResolver } from "@hookform/resolvers/zod"
import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import { ArrowRight, Loader2 } from "lucide-react"
import { useForm } from "react-hook-form"
import { z } from "zod"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { loginExpired } from "@/lib/login-return"
import { usernameSchema } from "@/lib/username"

const formSchema = z.object({
  username: usernameSchema,
  password: z.string().min(1, { message: "请输入密码" }),
})
type FormData = z.infer<typeof formSchema>

export const Route = createFileRoute("/login")({
  component: Login,
  beforeLoad: () => {
    if (isLoggedIn()) throw redirect({ to: "/" })
  },
  head: () => ({ meta: [{ title: "登录 · TT ADA" }] }),
})
function Login() {
  const { loginMutation } = useAuth()
  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: { username: "", password: "" },
  })
  const { errors } = form.formState
  return (
    <AuthLayout>
      <Card>
        <CardHeader>
          <CardTitle>
            <h1 className="workspace-title">登录 TT ADA</h1>
          </CardTitle>
          <CardDescription>使用管理员分配的账号登录。</CardDescription>
        </CardHeader>
        <CardContent>
          {loginExpired() && (
            <Alert className="mb-4">
              <AlertDescription>登录已过期，请重新登录。</AlertDescription>
            </Alert>
          )}
          <form
            noValidate
            onSubmit={form.handleSubmit((data) => {
              if (!loginMutation.isPending) loginMutation.mutate(data)
            })}
          >
            <FieldGroup>
              <Field data-invalid={!!errors.username}>
                <FieldLabel htmlFor="username">账号</FieldLabel>
                <Input
                  id="username"
                  data-testid="username-input"
                  type="text"
                  autoComplete="username"
                  placeholder="请输入账号"
                  aria-invalid={!!errors.username}
                  aria-describedby={
                    errors.username ? "username-error" : undefined
                  }
                  {...form.register("username")}
                />
                {errors.username && (
                  <FieldError id="username-error" errors={[errors.username]} />
                )}
              </Field>
              <Field data-invalid={!!errors.password}>
                <div className="flex items-center justify-between gap-3">
                  <FieldLabel htmlFor="password">密码</FieldLabel>
                  <Link
                    to="/recover-password"
                    className="text-xs text-primary hover:underline"
                  >
                    忘记密码？
                  </Link>
                </div>
                <Input
                  id="password"
                  data-testid="password-input"
                  type="password"
                  autoComplete="current-password"
                  placeholder="请输入密码"
                  aria-invalid={!!errors.password}
                  aria-describedby={
                    errors.password ? "password-error" : undefined
                  }
                  {...form.register("password")}
                />
                {errors.password && (
                  <FieldError id="password-error" errors={[errors.password]} />
                )}
              </Field>
              {loginMutation.isError && (
                <Alert variant="destructive">
                  <AlertDescription>
                    登录失败，请检查账号和密码后重试。
                  </AlertDescription>
                </Alert>
              )}
              <Field>
                <Button type="submit" disabled={loginMutation.isPending}>
                  {loginMutation.isPending ? (
                    <>
                      <Loader2
                        data-icon="inline-start"
                        className="animate-spin"
                      />
                      正在登录…
                    </>
                  ) : (
                    <>
                      登录 TT ADA
                      <ArrowRight data-icon="inline-end" />
                    </>
                  )}
                </Button>
              </Field>
            </FieldGroup>
          </form>
        </CardContent>
        <CardFooter>
          <p className="text-xs text-muted-foreground">
            账号由平台管理员开通。如需接入，请联系管理员。
          </p>
        </CardFooter>
      </Card>
    </AuthLayout>
  )
}
