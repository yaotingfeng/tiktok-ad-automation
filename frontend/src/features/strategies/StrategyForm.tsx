import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useBlocker, useNavigate } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { Loader2 } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  StrategiesService,
  type StrategyConfig_Output,
  type StrategyPublic,
  type VersionPublic,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { handleApiError } from "@/lib/api-feedback"
import { CopyPoolSheet } from "./CopyPoolSheet"
import { StrategyError as RequestError } from "./feedback"
import {
  copyPoolQuery,
  DEFAULT_COPY_POOL,
  strategyKey,
  strategyQuery,
} from "./queries"
import { StrategyNamingExample } from "./StrategyNamingExample"
import { StrategyStructureExample } from "./StrategyStructureExample"
import {
  configFingerprint,
  DEFAULT_SUFFIX,
  decimalError,
  issueMessages,
  suffixError,
} from "./validation"

const currencies = (
  Intl as typeof Intl & { supportedValuesOf: (key: string) => string[] }
).supportedValuesOf("currency")
const fieldNames: Record<string, string> = {
  budget: "Campaign 日预算",
  currency: "预算币种",
  target_roas: "目标 ROAS",
  group_size: "每组素材数量",
  creative_count: "创意数量",
  campaign_suffix: "Campaign 后缀模板",
  copy_pool_version: "文案池版本",
  cta_option_ids: "CTA 配置",
  name: "策略名称",
}
export function StrategyForm({
  strategy,
  version,
  copy = false,
  forceReadonly = false,
}: {
  strategy?: StrategyPublic
  version?: VersionPublic
  copy?: boolean
  forceReadonly?: boolean
}) {
  const { tenantId: currentTenantId, scope } = useTenantScope(),
    client = useQueryClient(),
    navigate = useNavigate()
  const [tenantId] = useState(currentTenantId!)
  const strategyId = copy ? undefined : strategy?.id,
    requestKey = `strategy-save-pending:${tenantId}:${strategyId || "new"}`
  const [initial] = useState(() => version?.config || strategy?.config)
  const [name, setName] = useState(
      copy ? `${strategy?.name || "策略"} 副本` : strategy?.name || "",
    ),
    [budget, setBudget] = useState(initial?.budget || ""),
    [currency, setCurrency] = useState(initial?.currency || ""),
    [roas, setRoas] = useState(initial?.target_roas || ""),
    [groupSize, setGroupSize] = useState(
      initial ? String(initial.group_size) : "",
    ),
    [creativeCount, setCreativeCount] = useState(
      initial ? String(initial.creative_count) : "",
    ),
    [suffix, setSuffix] = useState(initial?.campaign_suffix ?? DEFAULT_SUFFIX)
  const [baseline, setBaseline] = useState(initial),
    [baseNumber, setBaseNumber] = useState(
      version?.number || strategy?.latest_version || 0,
    ),
    [pending, setPending] = useState(false),
    [completed, setCompleted] = useState<VersionPublic>(),
    [unknownRequest, setUnknownRequest] = useState<string | null>(() =>
      sessionStorage.getItem(requestKey),
    ),
    [unknownNote, setUnknownNote] = useState(""),
    [error, setError] = useState<unknown>(),
    [serverErrors, setServerErrors] = useState<Record<string, string>>({}),
    [conflict, setConflict] = useState<StrategyPublic>(),
    [showConflict, setShowConflict] = useState(false),
    [poolOpen, setPoolOpen] = useState(false),
    [touched, setTouched] = useState<Record<string, boolean>>({}),
    [denied, setDenied] = useState(false)
  const copyPoolVersion = initial?.copy_pool_version || DEFAULT_COPY_POOL,
    ctaIds = initial?.cta_option_ids || []
  const pool = useQuery(copyPoolQuery(tenantId!, copyPoolVersion))
  const capacity = pool.data
    ? new Set(
        pool.data.entries
          .filter((row) => row.text.trim())
          .map((row) => row.text),
      ).size
    : undefined
  const cfg: StrategyConfig_Output = {
    budget,
    currency,
    target_roas: roas,
    group_size: Number(groupSize),
    creative_count: Number(creativeCount),
    copy_pool_version: copyPoolVersion,
    cta_option_ids: ctaIds,
    campaign_suffix: suffix,
  }
  const readonly =
    forceReadonly ||
    scope?.role === "viewer" ||
    denied ||
    (!copy && strategy?.active === false)
  const dirty = strategyId
    ? !!baseline && configFingerprint(cfg) !== configFingerprint(baseline)
    : !!name ||
      !!budget ||
      !!currency ||
      !!roas ||
      !!groupSize ||
      !!creativeCount ||
      suffix !== DEFAULT_SUFFIX
  const local: Record<string, string> = {}
  if (!name.trim()) local.name = "请填写策略名称。"
  if (name.length > 120) local.name = "名称不能超过 120 个字符。"
  const budgetIssue = decimalError(budget),
    roasIssue = decimalError(roas),
    nameIssue = suffixError(suffix)
  if (budgetIssue) local.budget = budgetIssue
  if (roasIssue) local.target_roas = roasIssue
  if (!/^[A-Z]{3}$/.test(currency)) local.currency = "请选择预算币种。"
  if (
    !/^\d+$/.test(groupSize) ||
    !Number.isSafeInteger(cfg.group_size) ||
    cfg.group_size < 1
  )
    local.group_size = "请输入大于 0 的整数。"
  if (
    !/^\d+$/.test(creativeCount) ||
    !Number.isSafeInteger(cfg.creative_count) ||
    cfg.creative_count < 1
  )
    local.creative_count = "请输入大于 0 的整数。"
  else if (capacity !== undefined && cfg.creative_count > capacity)
    local.creative_count = `创意数量不能超过 ${capacity} 条有效且不重复的英文文案。`
  if (nameIssue) local.campaign_suffix = nameIssue
  const errors = { ...local, ...serverErrors },
    firstError = Object.keys(errors)[0]
  useEffect(() => {
    if (
      strategy &&
      strategyId &&
      !readonly &&
      !completed &&
      strategy.latest_version !== baseNumber
    )
      setConflict(strategy)
  }, [strategy, strategyId, readonly, completed, baseNumber])
  const blockers = useBlocker({
    shouldBlockFn: () => !completed && (dirty || pending || !!unknownRequest),
    enableBeforeUnload: !completed && (dirty || pending || !!unknownRequest),
    withResolver: true,
  })
  const requestController = useRef<AbortController | null>(null),
    inFlight = useRef(false),
    checking = useRef(false)
  useEffect(() => () => requestController.current?.abort(), [])
  const finish = (saved: VersionPublic) => {
    sessionStorage.removeItem(requestKey)
    setUnknownRequest(null)
    setCompleted(saved)
    setPending(false)
    client.setQueryData([...strategyKey(tenantId!), "version", saved.id], saved)
    void client.invalidateQueries({ queryKey: strategyKey(tenantId!) })
    toast.success(`已保存 v${saved.number}`)
  }
  const checkSaved = async (id: string) => {
    if (checking.current) return
    checking.current = true
    requestController.current ||= new AbortController()
    setPending(true)
    setUnknownNote("")
    try {
      const { data } = await StrategiesService.savedRequest({
        path: { tenant_id: tenantId!, request_id: id },
        signal: requestController.current?.signal,
      })
      if (data?.request_id === id) finish(data)
      else setUnknownNote("返回记录尚不能证明本次保存，请继续确认。")
    } catch (e) {
      if (e instanceof Error) handleApiError(e)
      setUnknownNote("暂未确认这次保存记录。请继续回查，不会再次提交保存请求。")
    } finally {
      checking.current = false
      setPending(false)
    }
  }
  const checkRef = useRef(checkSaved)
  checkRef.current = checkSaved
  useEffect(() => {
    if (unknownRequest) void checkRef.current(unknownRequest)
  }, [unknownRequest])
  useEffect(() => {
    if (completed)
      void navigate({
        to: "/tenants/$tenantId/strategies/$strategyId",
        params: { tenantId: tenantId!, strategyId: completed.strategy_id },
        search: { bc_id: scope?.bcId || undefined, version_id: completed.id },
        replace: true,
      })
  }, [completed, navigate, tenantId, scope?.bcId])
  const save = async () => {
    if (
      inFlight.current ||
      readonly ||
      unknownRequest ||
      !dirty ||
      Object.keys(errors).length ||
      capacity === undefined
    )
      return
    inFlight.current = true
    setPending(true)
    setError(undefined)
    setServerErrors({})
    requestController.current = new AbortController()
    let attempted = false
    try {
      const validation = (
        await StrategiesService.validate({
          path: { tenant_id: tenantId! },
          body: cfg,
          signal: requestController.current.signal,
        })
      ).data!
      if (!validation.valid) {
        setServerErrors(
          Object.fromEntries(
            validation.errors.map((issue) => [
              issue.field,
              issueMessages[issue.code] || "该字段未通过策略校验。",
            ]),
          ),
        )
        return
      }
      const requestId = crypto.randomUUID()
      sessionStorage.setItem(requestKey, requestId)
      attempted = true
      const { data } = strategyId
        ? await StrategiesService.append({
            path: { tenant_id: tenantId!, strategy_id: strategyId },
            body: {
              config: cfg,
              expected_version: baseNumber,
              request_id: requestId,
            },
            signal: requestController.current.signal,
          })
        : await StrategiesService.create({
            path: { tenant_id: tenantId! },
            body: { name: name.trim(), config: cfg, request_id: requestId },
            signal: requestController.current.signal,
          })
      if (!data || data.request_id !== requestId) {
        setUnknownRequest(requestId)
        return
      }
      finish(data)
    } catch (e) {
      const status = e instanceof AxiosError ? e.response?.status : undefined
      if (attempted && (!status || status >= 500 || status === 408)) {
        setUnknownRequest(sessionStorage.getItem(requestKey))
        return
      }
      sessionStorage.removeItem(requestKey)
      setError(e)
      if (e instanceof Error) handleApiError(e)
      if (status === 403) {
        setDenied(true)
        void client.invalidateQueries({
          queryKey: ["tenant", tenantId, "scope"],
        })
      }
      const code = e instanceof AxiosError ? e.response?.data?.code : undefined
      if (status === 409 && strategyId) {
        try {
          setConflict(
            await client.fetchQuery({
              ...strategyQuery(tenantId!, strategyId),
              staleTime: 0,
            }),
          )
        } catch {
          /* Original input and error remain visible. */
        }
      }
      if (code === "copy_pool_exhausted")
        setServerErrors({ creative_count: issueMessages[code] })
      if (code === "invalid_name_template")
        setServerErrors({ campaign_suffix: issueMessages[code] })
    } finally {
      inFlight.current = false
      setPending(false)
    }
  }
  const change =
    (key: string, setter: (value: string) => void) => (value: string) => {
      setter(value)
      setTouched((old) => ({ ...old, [key]: true }))
      setServerErrors((old) => {
        const next = { ...old }
        delete next[key]
        return next
      })
    }
  const invalid = (key: string) =>
    !!errors[key] && (!!touched[key] || !!initial || !!serverErrors[key])
  const input = (
    key: string,
    value: string,
    setter: (value: string) => void,
    description?: string,
  ) => (
    <Field data-invalid={invalid(key)}>
      <FieldLabel htmlFor={`strategy-${key}`}>{fieldNames[key]}</FieldLabel>
      <Input
        id={`strategy-${key}`}
        value={value}
        onChange={(e) => change(key, setter)(e.target.value)}
        disabled={pending || !!unknownRequest}
        readOnly={readonly || (key === "name" && !!strategyId)}
        aria-invalid={invalid(key)}
        aria-describedby={`strategy-${key}-help`}
        inputMode={
          ["budget", "target_roas"].includes(key)
            ? "decimal"
            : ["group_size", "creative_count"].includes(key)
              ? "numeric"
              : undefined
        }
      />
      <FieldDescription id={`strategy-${key}-help`}>
        {description}
      </FieldDescription>
      {invalid(key) && <FieldError>{errors[key]}</FieldError>}
    </Field>
  )
  return (
    <>
      <div className="flex flex-col gap-2">
        <WorkspacePageTitle>
          {strategyId
            ? readonly
              ? "策略版本详情"
              : "编辑投放策略"
            : "新建投放策略"}
        </WorkspacePageTitle>
        <p className="text-sm text-muted-foreground">
          当前租户策略 · {strategy?.name || "新策略"}{" "}
          {baseNumber > 0 && (
            <Badge variant="outline">
              {copy ? "复制来源" : "来源版本"} v{baseNumber}
            </Badge>
          )}
        </p>
      </div>
      {strategyId && !readonly && (
        <Button
          className="self-start"
          variant="outline"
          size="sm"
          disabled={pending || !!unknownRequest}
          onClick={() =>
            void client.invalidateQueries({
              queryKey: strategyQuery(tenantId, strategyId).queryKey,
            })
          }
        >
          检查最新版本
        </Button>
      )}
      {!copy && strategy?.active === false && (
        <Alert>
          <AlertTitle>策略已停用</AlertTitle>
          <AlertDescription>
            可在列表恢复可用。已提交任务不受影响。
          </AlertDescription>
        </Alert>
      )}
      {unknownRequest && (
        <Alert>
          <AlertTitle>保存结果待确认</AlertTitle>
          <AlertDescription>
            <p>{unknownNote || "正在按本次保存请求 ID 精确回查结果…"}</p>
            <p className="break-all text-xs">请求 ID：{unknownRequest}</p>
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => void checkSaved(unknownRequest)}
            >
              确认保存结果
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {!!error && !conflict && <RequestError error={error} />}
      {conflict && (
        <Alert variant="destructive">
          <AlertTitle>服务器版本已变化</AlertTitle>
          <AlertDescription>
            <p>服务器当前为 v{conflict.latest_version}，本地输入已保留。</p>
            <Button variant="outline" onClick={() => setShowConflict(true)}>
              查看服务器差异
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {dirty && firstError && (
        <Alert variant="destructive">
          <AlertTitle>请检查策略字段</AlertTitle>
          <AlertDescription>
            <Button
              variant="link"
              className="h-auto max-w-full whitespace-normal px-0 text-left"
              onClick={() => {
                setTouched((old) => ({ ...old, [firstError]: true }))
                document.getElementById(`strategy-${firstError}`)?.focus()
              }}
            >
              {fieldNames[firstError] || firstError}：{errors[firstError]}
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <div className="grid w-full min-w-0 max-w-7xl items-start gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <form
          id="strategy-form"
          noValidate
          className="flex min-w-0 flex-col gap-6 pb-6"
          onSubmit={(e) => {
            e.preventDefault()
            void save()
          }}
        >
          <Card>
            <CardHeader>
              <CardTitle>基本信息</CardTitle>
              <CardDescription>
                每次有效修改保存为新的不可变版本。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                {input(
                  "name",
                  name,
                  setName,
                  strategyId
                    ? "已有策略名称只读；版本历史不会被覆盖。"
                    : "填写租户内便于识别的策略名称。",
                )}
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>预算与出价</CardTitle>
              <CardDescription>
                多个广告组共享这个 Campaign 的日预算。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup className="sm:grid sm:grid-cols-3">
                {input(
                  "budget",
                  budget,
                  setBudget,
                  "每个 Campaign / 天；金额按十进制字符串保存。",
                )}
                <Field data-invalid={invalid("currency")}>
                  <FieldLabel htmlFor="strategy-currency">预算币种</FieldLabel>
                  <Select
                    value={currency}
                    disabled={readonly || pending || !!unknownRequest}
                    onValueChange={change("currency", setCurrency)}
                  >
                    <SelectTrigger
                      id="strategy-currency"
                      aria-label="预算币种"
                      aria-invalid={invalid("currency")}
                    >
                      <SelectValue placeholder="选择币种" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {Array.from(
                          new Set([
                            ...currencies,
                            ...(currency ? [currency] : []),
                          ]),
                        )
                          .sort()
                          .map((code) => (
                            <SelectItem key={code} value={code}>
                              {code}
                            </SelectItem>
                          ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  <FieldDescription>
                    与目标账户币种匹配；不自动换算。
                  </FieldDescription>
                  {invalid("currency") && (
                    <FieldError>{errors.currency}</FieldError>
                  )}
                </Field>
                {input(
                  "target_roas",
                  roas,
                  setRoas,
                  "目标倍率，例如 1.08 倍；不表示百分比。",
                )}
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>素材与创意</CardTitle>
              <CardDescription>
                每个素材组对应一个 Ad Group；SP 使用同样素材、不同文案。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup className="sm:grid sm:grid-cols-2">
                {input(
                  "group_size",
                  groupSize,
                  setGroupSize,
                  "按文件名顺序分组，保留不足整组的尾组。",
                )}{" "}
                {input(
                  "creative_count",
                  creativeCount,
                  setCreativeCount,
                  "每个 Ad Group 的普通 Smart+ Ad 数量。",
                )}
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>文案与 CTA</CardTitle>
              <CardDescription>
                平台英文文案池；同一组无放回抽取不同正文。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                <Field>
                  <FieldLabel>文案池版本</FieldLabel>
                  <p
                    id="strategy-copy_pool_version"
                    tabIndex={-1}
                    className="break-all text-sm"
                  >
                    {pool.data?.name || "正在读取文案池…"} · {copyPoolVersion}
                  </p>
                  {pool.error && (
                    <RequestError
                      error={pool.error}
                      retry={() => void pool.refetch()}
                    />
                  )}
                  <FieldDescription>
                    {capacity === undefined
                      ? "尚未取得有效文案数量。"
                      : `当前有效去重正文：${capacity} 条 · 英文`}
                  </FieldDescription>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setPoolOpen(true)}
                  >
                    查看文案
                  </Button>
                  {serverErrors.copy_pool_version && (
                    <FieldError>{serverErrors.copy_pool_version}</FieldError>
                  )}
                </Field>
                <Field>
                  <FieldLabel>CTA 配置</FieldLabel>
                  <p className="text-sm">CTA 候选尚未就绪，待场景核实。</p>
                  {ctaIds.length ? (
                    <div className="flex flex-wrap gap-1">
                      {ctaIds.map((id) => (
                        <Badge
                          key={id}
                          variant="outline"
                          className="max-w-full whitespace-normal break-all"
                        >
                          已有 ID：{id}
                        </Badge>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      尚未配置 CTA 选项。
                    </p>
                  )}
                  <FieldDescription>
                    保留已有选项；具体账户的可用 CTA 将在搭建预览中校验。
                  </FieldDescription>
                  {serverErrors.cta_option_ids && (
                    <FieldError>{serverErrors.cta_option_ids}</FieldError>
                  )}
                </Field>
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>广告命名</CardTitle>
              <CardDescription>
                归因基础名来自版权方，只编辑 Campaign 后缀。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                {input(
                  "campaign_suffix",
                  suffix,
                  setSuffix,
                  "必须包含批次号变量。广告组继承 Campaign 名称-g{group_no}，广告继承广告组名称-sp{creative_no}。",
                )}
                {!readonly && (
                  <div className="flex flex-wrap gap-2">
                    {["{YYYYMMDD}", "{batch_short_id}"].map((variable) => (
                      <Button
                        key={variable}
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={pending || !!unknownRequest}
                        onClick={() =>
                          change(
                            "campaign_suffix",
                            setSuffix,
                          )(suffix + variable)
                        }
                      >
                        插入 {variable}
                      </Button>
                    ))}
                  </div>
                )}
              </FieldGroup>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>生成规则</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2 text-sm">
              <p>
                每剧每户 1 个 Campaign；每个素材组 1 个 Ad Group；每组 N 条普通
                Ad。
              </p>
              <p>本批所有有效剧目覆盖全部有效目标账户。策略不保存账户池。</p>
              <p>提交具体搭建预览后，Campaign、Ad Group、Ad 直接启用。</p>
            </CardContent>
          </Card>
        </form>
        <aside className="flex min-w-0 flex-col gap-6">
          <StrategyStructureExample
            groupSize={/^\d+$/.test(groupSize) ? cfg.group_size : Number.NaN}
            creativeCount={
              /^\d+$/.test(creativeCount) ? cfg.creative_count : Number.NaN
            }
            budget={budgetIssue ? "" : budget}
            currency={currency}
          />
          <StrategyNamingExample suffix={suffix} />
        </aside>
      </div>
      <div className="sticky bottom-0 flex w-full max-w-7xl flex-wrap items-center justify-between gap-3 border-t bg-background px-4 py-3">
        <p className="text-sm text-muted-foreground">
          {readonly
            ? "只读版本，已提交任务继续使用原配置。"
            : !dirty
              ? "没有未保存的修改。"
              : "保存新版本后，搭建草稿需重新生成预览。"}
        </p>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={pending}
            onClick={() =>
              void navigate({
                to: "/tenants/$tenantId/strategies",
                params: { tenantId: tenantId! },
                search: { bc_id: scope?.bcId || undefined },
              })
            }
          >
            {readonly ? "返回列表" : "取消"}
          </Button>
          {!readonly && (
            <Button
              type="submit"
              form="strategy-form"
              disabled={
                pending ||
                !!unknownRequest ||
                !!conflict ||
                !dirty ||
                !!firstError ||
                capacity === undefined ||
                !!pool.error
              }
            >
              {pending && (
                <Loader2 className="animate-spin" data-icon="inline-start" />
              )}
              {pending ? "正在保存…" : strategyId ? "保存为新版本" : "创建策略"}
            </Button>
          )}
        </div>
      </div>
      {poolOpen && (
        <CopyPoolSheet
          tenantId={tenantId!}
          versionId={copyPoolVersion}
          onClose={() => setPoolOpen(false)}
        />
      )}{" "}
      {showConflict && conflict && (
        <ManagementSheet
          title="服务器版本差异"
          description={`本地基于 v${baseNumber}，服务器当前 v${conflict.latest_version}。原版本保留。`}
          dirty={false}
          onClose={() => setShowConflict(false)}
        >
          <div className="flex flex-col gap-4">
            <dl className="flex flex-col gap-3">
              {Object.entries(fieldNames)
                .filter(([key]) => key !== "name")
                .map(([key, label]) => (
                  <div key={key}>
                    <dt className="font-semibold">{label}</dt>
                    <dd className="break-all text-sm">
                      本地：{String(cfg[key as keyof typeof cfg])}
                      <br />
                      服务器：{String(conflict.config[key as keyof typeof cfg])}
                    </dd>
                  </div>
                ))}
            </dl>
            <Button
              onClick={() => {
                setBaseNumber(conflict.latest_version)
                setBaseline(conflict.config)
                setConflict(undefined)
                setShowConflict(false)
                setError(undefined)
              }}
            >
              以服务器版本为基线，保留本地编辑
            </Button>
          </div>
        </ManagementSheet>
      )}
      <Dialog
        open={blockers.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blockers.reset?.()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>有未保存的修改</DialogTitle>
            <DialogDescription>
              {unknownRequest
                ? "本次保存结果尚未确认，离开不会撤销服务器可能已完成的保存。"
                : "离开会丢弃当前表单的未保存修改，已保存版本会保留。"}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => blockers.reset?.()}>
              留在当前页
            </Button>
            <Button disabled={pending} onClick={() => blockers.proceed?.()}>
              丢弃未保存修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
