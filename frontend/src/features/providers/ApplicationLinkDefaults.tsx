import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import {
  type ProviderApplicationPublic,
  type ProviderConnectionPublic,
  ProvidersService,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { ProviderLinkConfigFields } from "@/features/builds/ProviderLinkConfigFields"
import { capabilitiesQuery, providerKey } from "./queries"

// 推广应用的取链默认值：每次搭建自动带入，搭建页可单次调整覆盖。
export function ApplicationLinkDefaults({
  tenantId,
  connection,
  application,
}: {
  tenantId: string
  connection: ProviderConnectionPublic
  application: ProviderApplicationPublic
}) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState<Record<string, number>>({})
  const client = useQueryClient()
  const schema = useQuery({
    ...capabilitiesQuery(tenantId, connection.id, application.external_id),
    enabled: open,
  })
  const save = useMutation({
    mutationFn: async () =>
      (
        await ProvidersService.patchApplicationLinkDefaults({
          path: {
            tenant_id: tenantId,
            connection_id: connection.id,
            application_id: application.external_id,
          },
          body: { link_defaults: value },
          throwOnError: true,
        })
      ).data,
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: providerKey(tenantId) })
      setOpen(false)
    },
  })
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (next) {
          save.reset()
          setValue({ ...(application.link_defaults as Record<string, number>) })
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" disabled={!application.available}>
          取链默认值
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{application.name} · 取链默认值</DialogTitle>
        </DialogHeader>
        {schema.data &&
          (schema.data.fields.some(
            (field) => field.source === "application_default",
          ) ? (
            <ProviderLinkConfigFields
              schema={schema.data}
              value={value}
              mode="defaults"
              disabled={save.isPending}
              onChange={setValue}
            />
          ) : (
            <p className="text-sm text-muted-foreground">
              参数与版权方后台一致，无需设置
            </p>
          ))}
        {save.error && (
          <p className="text-sm text-destructive">保存失败，请检查参数后重试</p>
        )}
        <DialogFooter>
          <Button
            onClick={() => save.mutate()}
            disabled={save.isPending || !schema.data}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
