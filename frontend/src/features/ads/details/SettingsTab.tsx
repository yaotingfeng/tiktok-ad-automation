import { useState } from "react"
import { Button } from "@/components/ui/button"
import { type ConfigKind, configSections } from "./configFields"

function observedText(observedAt: string) {
  const time = Date.parse(observedAt)
  return Number.isFinite(time)
    ? new Date(time).toLocaleString("zh-CN", { hour12: false })
    : observedAt
}

export function SettingsTab({
  kind,
  configuration,
  observedAt,
}: {
  kind: ConfigKind
  configuration: Record<string, unknown>
  observedAt: string
}) {
  const [showOther, setShowOther] = useState(false)
  const sections = configSections(kind, configuration)
  return (
    <div className="flex flex-col gap-5 text-sm">
      {/* 配置来自目录同步，不是实时读取 TikTok。 */}
      <p className="text-xs text-muted-foreground">
        配置来自最近一次目录同步：{observedText(observedAt)}
      </p>
      {sections.length === 0 && (
        <p className="text-muted-foreground">暂无同步到的配置</p>
      )}
      {sections.map((section) => {
        const other = section.title === "其他字段"
        return (
          <section key={section.title} className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <h3 className="font-medium">{section.title}</h3>
              {other && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-expanded={showOther}
                  onClick={() => setShowOther(!showOther)}
                >
                  {showOther ? "收起" : `展开（${section.items.length}）`}
                </Button>
              )}
            </div>
            {(!other || showOther) && (
              <dl className="grid grid-cols-1 gap-x-6 gap-y-2 md:grid-cols-2">
                {section.items.map((item) => (
                  <div
                    key={item.label}
                    className="grid grid-cols-[9rem_1fr] gap-2 border-b py-1"
                  >
                    <dt className="text-muted-foreground">{item.label}</dt>
                    <dd className="whitespace-pre-line break-all">
                      {item.value}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </section>
        )
      })}
    </div>
  )
}
