import { useQuery } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import { flushSync } from "react-dom"
import {
  BuildsService,
  type DraftSummary,
  type ProviderConnectionPublic,
  ProvidersService,
  StrategiesService,
  type StrategyPublic,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { versionQuery } from "@/features/strategies/queries"
import { normalizeDecimal } from "@/features/strategies/validation"
import { DirectoryPicker } from "@/features/tenants/DirectoryPicker"
import { ApplicationPicker } from "./ApplicationPicker"
import { mutationKey } from "./api"
import { DraftConflict } from "./DraftConflict"
import { ManualLinksDialog } from "./ManualLinksDialog"
import { linksForLines, type NamedManualLink } from "./manualLinks"
import {
  BuildError,
  BuildGuard,
  BuildSteps,
  reportError,
  unknownOutcome,
} from "./presentation"

type Values = {
  customProvider: string
  manualLinks: NamedManualLink[]
  drama: string
  account: string
  connection: string
  application: string
  version: string
}
type Pending = { requestId: string; prepare: boolean; values: Values }
export function BuildInputPage({
  tenantId,
  bcId,
  write,
  summary,
  original,
  onSaved,
  onCancel,
}: {
  tenantId: string
  bcId: string
  write: boolean
  summary?: DraftSummary
  original?: {
    drama: string[]
    account: string[]
    manualLinks?: NamedManualLink[]
  }
  onSaved?: (prepare: boolean) => void
  onCancel?: () => void
}) {
  const key = summary
    ? mutationKey(tenantId, bcId, summary.draft_id)
    : `build-create:${tenantId}:${bcId}`
  const [pending, setPending] = useState<Pending | null>(() => {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) || "null")
      return value && typeof value.requestId === "string" ? value : null
    } catch {
      return null
    }
  })
  const initial = useRef<Values>({
    drama: original?.drama.join("\n") || "",
    account: original?.account.join("\n") || "",
    connection: summary?.custom_provider_name
      ? "other"
      : summary?.provider_connection_id || "",
    customProvider: summary?.custom_provider_name || "",
    manualLinks: original?.manualLinks || [],
    application: summary?.application_id || "",
    version: summary?.strategy_version_id || "",
  })
  const [values, setValues] = useState<Values>({
      ...initial.current,
      ...pending?.values,
    }),
    [labels, setLabels] = useState<Record<string, string>>({}),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    [forbidden, setForbidden] = useState(false),
    [leaving, setLeaving] = useState(false),
    [revision, setRevision] = useState(summary?.revision),
    [manualOpen, setManualOpen] = useState(false),
    [inputError, setInputError] = useState("")
  const controller = useRef(new AbortController()),
    navigate = useNavigate()
  useEffect(() => {
    const ctrl = new AbortController()
    controller.current = ctrl
    return () => ctrl.abort()
  }, [])
  const version = useQuery({
    ...versionQuery(tenantId, values.version),
    enabled: !!values.version,
  })
  const dirty = JSON.stringify(values) !== JSON.stringify(initial.current)
  const disabled = busy || !!pending || !write || forbidden
  const change = (patch: Partial<Values>) =>
    setValues((v) => ({ ...v, ...patch }))
  async function finish(draftId: string, prepare: boolean) {
    if (prepare)
      sessionStorage.setItem(`build-start:${tenantId}:${bcId}:${draftId}`, "1")
    sessionStorage.removeItem(key)
    setPending(null)
    flushSync(() => setLeaving(true))
    await navigate({
      to: "/tenants/$tenantId/build-drafts/$draftId",
      params: { tenantId, draftId },
      search: { bc_id: bcId, prepare: prepare ? true : undefined },
    })
  }
  async function recover() {
    if (!pending || busy) return
    setBusy(true)
    setError(undefined)
    try {
      const { data } = await (summary
        ? BuildsService.savedMutation
        : BuildsService.savedRequest)({
        path: { tenant_id: tenantId, request_id: pending.requestId },
        signal: controller.current.signal,
      })
      if (!controller.current.signal.aborted) {
        if (summary) {
          sessionStorage.removeItem(key)
          setPending(null)
          flushSync(() => setLeaving(true))
          onSaved?.(pending.prepare && write && !forbidden)
        } else
          await finish(data.draft_id, pending.prepare && write && !forbidden)
      }
    } catch (e) {
      if (!controller.current.signal.aborted) {
        reportError(e)
        setError(e)
      }
    } finally {
      setBusy(false)
    }
  }
  async function save(prepare: boolean) {
    if (
      disabled ||
      !values.version ||
      !values.connection ||
      (values.connection === "other"
        ? !values.customProvider.trim()
        : !values.application) ||
      !values.drama.trim() ||
      !values.account.trim()
    )
      return
    let manualLinks: ReturnType<typeof linksForLines>
    try {
      manualLinks = linksForLines(values.drama, values.manualLinks)
      setInputError("")
    } catch (e) {
      setInputError(e instanceof Error ? e.message : "请检查剧目与链接")
      return
    }
    setBusy(true)
    setError(undefined)
    try {
      if (summary) {
        // 编辑旧草稿后统一使用 BC 配置；原预览和已执行任务的冻结授权不变。
        if (!dirty && !summary.execution_connection_id) {
          onSaved?.(prepare)
          return
        }
        const operation = {
          requestId: crypto.randomUUID(),
          kind: "input",
          prepare,
          values,
        }
        sessionStorage.setItem(key, JSON.stringify(operation))
        setPending(operation)
        try {
          await BuildsService.update({
            path: { tenant_id: tenantId, draft_id: summary.draft_id },
            body: {
              request_id: operation.requestId,
              expected_revision: revision!,
              strategy_version_id: values.version,
              provider_connection_id:
                values.connection === "other" ? null : values.connection,
              custom_provider_name:
                values.connection === "other"
                  ? values.customProvider.trim()
                  : null,
              manual_links: manualLinks,
              execution_connection_id: null,
              application_id:
                values.connection === "other" ? null : values.application,
              drama_lines: values.drama.split("\n"),
              account_lines: values.account.split("\n"),
            },
            signal: controller.current.signal,
          })
          if (!controller.current.signal.aborted) {
            sessionStorage.removeItem(key)
            setPending(null)
            flushSync(() => setLeaving(true))
            onSaved?.(prepare)
          }
        } catch (e) {
          if (!unknownOutcome(e)) {
            sessionStorage.removeItem(key)
            setPending(null)
          }
          throw e
        }
      } else {
        const operation = { requestId: crypto.randomUUID(), prepare, values }
        sessionStorage.setItem(key, JSON.stringify(operation))
        setPending(operation)
        try {
          const { data } = await BuildsService.create({
            path: { tenant_id: tenantId },
            body: {
              request_id: operation.requestId,
              bc_id: bcId,
              strategy_version_id: values.version,
              provider_connection_id:
                values.connection === "other" ? null : values.connection,
              custom_provider_name:
                values.connection === "other"
                  ? values.customProvider.trim()
                  : null,
              manual_links: manualLinks,
              execution_connection_id: null,
              application_id:
                values.connection === "other" ? null : values.application,
              drama_lines: values.drama.split("\n"),
              account_lines: values.account.split("\n"),
              link_config: {},
            },
            signal: controller.current.signal,
          })
          if (!controller.current.signal.aborted)
            await finish(data.draft_id, prepare)
        } catch (e) {
          if (!unknownOutcome(e)) {
            sessionStorage.removeItem(key)
            setPending(null)
          }
          throw e
        }
      }
    } catch (e) {
      if (!controller.current.signal.aborted) {
        reportError(e)
        setError(e)
        if ((e as { response?: { status: number } }).response?.status === 403)
          setForbidden(true)
      }
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex w-full min-w-0 max-w-7xl flex-col gap-6">
      <BuildSteps step={1} />
      <BuildGuard dirty={!leaving && (dirty || busy || !!pending)} />
      {!!error && <BuildError error={error} />}
      {inputError && (
        <Alert variant="destructive">
          <AlertDescription>{inputError}</AlertDescription>
        </Alert>
      )}
      {manualOpen && (
        <ManualLinksDialog
          links={values.manualLinks}
          onClose={() => setManualOpen(false)}
          onSave={(links) => {
            const lines = values.drama ? values.drama.split("\n") : []
            for (const link of links) {
              const matches = lines.filter((line) => line.trim() === link.title)
              if (matches.length > 1)
                throw new Error(
                  `「${link.title}」在剧目输入中重复，请先合并为一行`,
                )
              if (!matches.length) lines.push(link.title)
            }
            if (lines.length > 1000) throw new Error("剧目总数最多 1000 行")
            change({ drama: lines.join("\n"), manualLinks: links })
          }}
        />
      )}
      {summary &&
        error instanceof AxiosError &&
        error.response?.status === 409 && (
          <DraftConflict
            tenantId={tenantId}
            draftId={summary.draft_id}
            onApply={(revision) => {
              setRevision(revision)
              setError(undefined)
            }}
          />
        )}{" "}
      {pending && (
        <Alert>
          <AlertDescription>
            <p>正在确认原草稿保存结果。原请求标识已保留，不会重复创建。</p>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void recover()}
            >
              {summary ? "查询原修改结果" : "查询原保存结果"}
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!write && <p role="status">当前为只读角色，可以查看已保存草稿。</p>}
      <Card className="min-w-0">
        <CardContent>
          <FieldGroup className="grid min-w-0 gap-6 md:grid-cols-3">
            <p className="text-sm text-muted-foreground md:col-span-3">
              使用当前 BC 设置的授权，在开始准备时固定。如需更换，请前往
              <Link
                className="underline underline-offset-4"
                to="/tenants/$tenantId/accounts"
                params={{ tenantId }}
                search={{ bc_id: bcId, tab: "accounts" }}
              >
                账户与授权
              </Link>
              。
            </p>
            <Field>
              <FieldLabel>版权方连接</FieldLabel>
              <DirectoryPicker<ProviderConnectionPublic>
                label="版权方连接"
                selectedId={values.connection}
                valueLabel={
                  values.connection === "other"
                    ? "其他版权方"
                    : labels.connection || values.connection
                }
                alternative={{
                  label: "其他版权方",
                  description: "填写版权方名称，使用已有推广链接。",
                  selected: values.connection === "other",
                  onSelect: () => {
                    change({ connection: "other", application: "" })
                    setLabels((l) => ({
                      ...l,
                      connection: "",
                      application: "",
                    }))
                  },
                }}
                disabled={disabled}
                queryKey={["tenant", tenantId, "builds", "provider-picker"]}
                load={async (query, cursor, limit, signal) =>
                  (
                    await ProvidersService.listConnections({
                      path: { tenant_id: tenantId },
                      query: { query, cursor, limit, status: "active" },
                      signal,
                    })
                  ).data
                }
                renderItem={(item) => <span>{item.display_name}</span>}
                onSelect={(item) => {
                  change({ connection: item.id, application: "" })
                  setLabels((l) => ({
                    ...l,
                    connection: item.display_name,
                    application: "",
                  }))
                }}
              />
            </Field>
            {values.connection === "other" ? (
              <Field>
                <FieldLabel htmlFor="custom-provider-name">
                  版权方名称
                </FieldLabel>
                <Input
                  id="custom-provider-name"
                  value={values.customProvider}
                  maxLength={100}
                  disabled={disabled}
                  onChange={(e) => change({ customProvider: e.target.value })}
                  placeholder="填写实际版权方名称"
                />
                <p className="text-xs text-muted-foreground">
                  无需连接后台，使用你提供的推广链接。
                </p>
              </Field>
            ) : (
              <Field>
                <FieldLabel>推广应用</FieldLabel>
                <ApplicationPicker
                  tenantId={tenantId}
                  connectionId={values.connection}
                  value={labels.application || values.application}
                  disabled={disabled || !values.connection}
                  onSelect={(item) => {
                    change({ application: item.external_id })
                    setLabels((l) => ({ ...l, application: item.name }))
                  }}
                />
                <p className="text-xs text-muted-foreground">
                  仅使用当前连接下的可用推广应用。
                </p>
              </Field>
            )}
            <Field>
              <FieldLabel>投放策略</FieldLabel>
              <DirectoryPicker<StrategyPublic>
                label="投放策略"
                valueLabel={labels.version || values.version}
                disabled={disabled}
                queryKey={["tenant", tenantId, "builds", "strategy-picker"]}
                load={async (query, cursor, limit, signal) =>
                  (
                    await StrategiesService.getStrategies({
                      path: { tenant_id: tenantId },
                      query: { query, cursor, limit, active: true },
                      signal,
                    })
                  ).data
                }
                renderItem={(item) => (
                  <span>
                    {item.name} · v{item.latest_version}
                  </span>
                )}
                onSelect={(item) => {
                  change({ version: item.version_id })
                  setLabels((l) => ({
                    ...l,
                    version: `${item.name} · v${item.latest_version}`,
                  }))
                }}
              />
            </Field>
          </FieldGroup>
        </CardContent>
      </Card>
      <div className="grid min-w-0 gap-6 md:grid-cols-2">
        {(["drama", "account"] as const).map((kind) => (
          <Card key={kind} className="min-w-0">
            <CardContent className="flex min-w-0 flex-col gap-3">
              <Field>
                <FieldLabel htmlFor={`build-${kind}`}>
                  {kind === "drama" ? "剧目名称" : "广告账户"}
                </FieldLabel>
                <Textarea
                  id={`build-${kind}`}
                  className="min-h-44 resize-y font-mono"
                  spellCheck={false}
                  value={values[kind]}
                  disabled={disabled}
                  onChange={(e) => change({ [kind]: e.target.value })}
                />
              </Field>
              {kind === "drama" && (
                <>
                  <Button
                    type="button"
                    variant="link"
                    className="self-start"
                    disabled={disabled}
                    onClick={() => setManualOpen(true)}
                  >
                    已有推广链接？批量添加
                  </Button>
                  {!!values.manualLinks.length && (
                    <p className="text-sm">
                      已添加{" "}
                      {
                        values.drama.split("\n").filter((line) => line.trim())
                          .length
                      }{" "}
                      部剧，其中 {values.manualLinks.length} 部已填写推广链接
                    </p>
                  )}
                  {values.connection === "other" && (
                    <p className="text-xs text-muted-foreground">
                      其他版权方需提供推广链接，可现在批量添加，也可在第二步补充。
                    </p>
                  )}
                </>
              )}
              <p className="text-xs text-muted-foreground">
                {values[kind] ? values[kind].split("\n").length : 0}{" "}
                行（输入计数，尚未核实） ·{" "}
                {kind === "drama"
                  ? "每行一部完整剧名；素材文件名包含剧名即可自动匹配。"
                  : "每行一个完整账户 ID 或完整名称；重复账户将自动去重。"}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Alert>
        <AlertDescription>全部剧目将覆盖同一批全部有效账户</AlertDescription>
      </Alert>
      {version.data && (
        <Card className="min-w-0">
          <CardHeader className="min-w-0">
            <CardTitle>
              <h2>策略摘要 · v{version.data.number}</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm">
            <p>
              每个 Campaign 日预算 {version.data.config.currency}{" "}
              {normalizeDecimal(version.data.config.budget)} · 目标 ROAS{" "}
              {version.data.config.target_roas} · 每组{" "}
              {version.data.config.group_size} 份素材 ·{" "}
              {version.data.config.creative_count} 条 SP 创意
            </p>
          </CardContent>
        </Card>
      )}
      <Card className="sticky bottom-0 min-w-0">
        <CardContent className="flex min-w-0 flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            解析剧目与账户，复用或获取推广链接；此步尚未创建广告。
          </p>
          <div className="flex flex-wrap gap-2">
            {onCancel && (
              <Button variant="outline" disabled={busy} onClick={onCancel}>
                返回准备
              </Button>
            )}
            {write && (
              <>
                <Button
                  variant="outline"
                  disabled={
                    disabled ||
                    !values.version ||
                    (values.connection === "other"
                      ? !values.customProvider.trim()
                      : !values.application) ||
                    !values.drama.trim() ||
                    !values.account.trim()
                  }
                  onClick={() => void save(false)}
                >
                  保存草稿
                </Button>
                <Button
                  disabled={
                    disabled ||
                    !values.version ||
                    (values.connection === "other"
                      ? !values.customProvider.trim()
                      : !values.application) ||
                    !values.drama.trim() ||
                    !values.account.trim()
                  }
                  onClick={() => void save(true)}
                >
                  {busy ? "正在保存…" : "解析并准备"}
                </Button>
              </>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
