import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { AutomationRulesService } from "@/client"
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
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { PasswordInput } from "@/components/ui/password-input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { canManage, errorMessage } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { ruleKeys, settingsQuery } from "./queries"

const TIMEZONES = [
  "Asia/Shanghai",
  "Asia/Hong_Kong",
  "UTC",
  "America/Los_Angeles",
  "America/New_York",
  "Europe/London",
]

export function NotificationSettingsPage() {
  const { tenantId, scope } = useTenantScope()
  const client = useQueryClient()
  const allowed = canManage(scope?.role)
  const settings = useQuery({ ...settingsQuery(tenantId!), enabled: allowed })
  const [webhook, setWebhook] = useState("")
  const [secret, setSecret] = useState("")
  const [enabled, setEnabled] = useState(true)
  const [timezone, setTimezone] = useState("Asia/Shanghai")
  const [paused, setPaused] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(
    null,
  )
  useEffect(() => {
    if (!settings.data) return
    setEnabled(
      settings.data.feishu_configured ? settings.data.feishu_enabled : true,
    )
    setTimezone(settings.data.schedule_timezone)
    setPaused(settings.data.paused)
  }, [settings.data])
  const feedback = (ok: boolean, text: string) => setNotice({ ok, text })
  const test = useMutation({
    mutationFn: () =>
      AutomationRulesService.testFeishu({
        path: { tenant_id: tenantId! },
        body: webhook.trim()
          ? { webhook_url: webhook.trim(), secret: secret.trim() || null }
          : {},
      }),
    onSuccess: () => feedback(true, "测试消息已发送，请在飞书群里查看。"),
    onError: (err) => feedback(false, errorMessage(err)),
  })
  const save = useMutation({
    mutationFn: (clear: boolean) =>
      AutomationRulesService.putSettings({
        path: { tenant_id: tenantId! },
        body: {
          schedule_timezone: timezone,
          paused,
          feishu_enabled: enabled,
          // 留空表示保持已保存的 Webhook 与密钥不变。
          webhook_url: webhook.trim() || null,
          secret: webhook.trim() ? secret.trim() || null : null,
          clear_feishu: clear,
        },
      }),
    onSuccess: async (_, clear) => {
      setWebhook("")
      setSecret("")
      await client.invalidateQueries({ queryKey: ruleKeys(tenantId!) })
      feedback(true, clear ? "飞书配置已删除。" : "已保存。")
    },
    onError: (err) => feedback(false, errorMessage(err)),
  })
  if (!allowed)
    return (
      <Alert>
        <AlertDescription>只有租户管理员可以修改通知设置。</AlertDescription>
      </Alert>
    )
  const data = settings.data
  return (
    <section aria-label="通知设置" className="flex max-w-3xl flex-col gap-6">
      <div className="flex flex-col gap-1">
        <WorkspacePageTitle>通知设置</WorkspacePageTitle>
        <p className="text-sm text-muted-foreground">
          自动规则的预警、待确认与执行结果通过租户飞书群机器人发送卡片。
        </p>
      </div>
      {notice && (
        <Alert variant={notice.ok ? "default" : "destructive"}>
          <AlertDescription>{notice.text}</AlertDescription>
        </Alert>
      )}
      <Card>
        <CardHeader>
          <CardTitle>飞书群机器人</CardTitle>
          <CardDescription>
            {data?.feishu_configured
              ? `已配置：${data.feishu_url_hint}${data.feishu_has_secret ? "（已设置签名密钥）" : ""}`
              : "尚未配置。在飞书群设置 → 群机器人 → 添加自定义机器人，复制 Webhook 地址。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Field>
            <FieldLabel htmlFor="feishu-webhook">Webhook 地址</FieldLabel>
            <Input
              id="feishu-webhook"
              aria-label="Webhook 地址"
              placeholder={
                data?.feishu_configured
                  ? "留空表示保持当前地址"
                  : "https://open.feishu.cn/open-apis/bot/v2/hook/..."
              }
              value={webhook}
              onChange={(e) => setWebhook(e.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="feishu-secret">签名密钥（可选）</FieldLabel>
            <PasswordInput
              id="feishu-secret"
              aria-label="签名密钥"
              placeholder="机器人开启「签名校验」时填写"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
            />
          </Field>
          <div className="flex items-center gap-2">
            <Checkbox
              id="feishu-enabled"
              checked={enabled}
              onCheckedChange={(checked) => setEnabled(!!checked)}
            />
            <Label htmlFor="feishu-enabled">启用飞书通知</Label>
          </div>
        </CardContent>
        <CardFooter className="flex gap-2">
          <Button
            variant="outline"
            disabled={
              test.isPending || (!webhook.trim() && !data?.feishu_configured)
            }
            onClick={() => test.mutate()}
          >
            发送测试消息
          </Button>
          {data?.feishu_configured && (
            <Button
              variant="ghost"
              disabled={save.isPending}
              onClick={() => save.mutate(true)}
            >
              删除配置
            </Button>
          )}
        </CardFooter>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>运行设置</CardTitle>
          <CardDescription>
            自定义运行时段按这里的时区解释；各账户的「今天」仍按账户自身时区计算。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Field className="w-64">
            <FieldLabel>运行时区</FieldLabel>
            <Select value={timezone} onValueChange={setTimezone}>
              <SelectTrigger aria-label="运行时区">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TIMEZONES.map((zone) => (
                  <SelectItem key={zone} value={zone}>
                    {zone}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <div className="flex items-center gap-2">
            <Checkbox
              id="rules-paused"
              checked={paused}
              onCheckedChange={(checked) => setPaused(!!checked)}
            />
            <Label htmlFor="rules-paused">暂停全部规则</Label>
          </div>
        </CardContent>
        <CardFooter>
          <Button disabled={save.isPending} onClick={() => save.mutate(false)}>
            保存
          </Button>
        </CardFooter>
      </Card>
    </section>
  )
}
