import { ChevronRight, CircleAlert } from "lucide-react"
import type { ReportRow } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  type AdsDimension,
  childDimension,
  nameKeyFor,
  type ParentLink,
  parentPath,
  rowName,
  rowObjectId,
} from "./search"

// 行覆盖状态里这两种都表示该行数据已同步完整（后者是平台确认的零消耗）。
const COMPLETE_STATUSES = new Set(["COMPLETE", "COMPLETE_EMPTY"])

function incompleteText(row: ReportRow): string | null {
  const coverage = row.coverage ?? {}
  const status = coverage.status as string | undefined
  const advertisers =
    (coverage.incomplete_advertiser_ids as string[] | undefined) ?? []
  if ((!status || COMPLETE_STATUSES.has(status)) && advertisers.length === 0)
    return null
  const dates = (coverage.stale_dates as string[] | undefined) ?? []
  return dates.length
    ? `数据不完整，缺失日期：${dates.join("、")}`
    : "该行数据尚未同步完整"
}

export function rowDisplayName(row: ReportRow, dimension: AdsDimension) {
  return row.display?.[nameKeyFor(dimension)] ?? rowName(row)
}

export function AdsNameCell({
  row,
  dimension,
  onOpen,
  onDrill,
  onFilterParent,
}: {
  row: ReportRow
  dimension: AdsDimension
  onOpen: (row: ReportRow) => void
  onDrill: (row: ReportRow) => void
  onFilterParent: (link: ParentLink, advertiserId: string) => void
}) {
  const display = row.display ?? {}
  const name = rowDisplayName(row, dimension)
  const id = rowObjectId(row, dimension)
  const path = parentPath(row, dimension)
  const advertiserId =
    display.account_id ??
    display.advertiser_id ??
    row.refs?.[0]?.advertiser_id ??
    ""
  const incomplete = incompleteText(row)
  return (
    <div className="flex min-w-0 items-start gap-1">
      <div className="min-w-0 flex-1">
        <button
          type="button"
          className="line-clamp-2 break-words text-left text-sm font-medium hover:underline"
          title={name}
          onClick={() => onOpen(row)}
        >
          {name}
        </button>
        {id && id !== name && (
          <div className="truncate text-[11px] text-muted-foreground">{id}</div>
        )}
        {path.length > 0 && (
          <div className="flex flex-wrap items-center gap-x-1 text-[11px] text-muted-foreground">
            {path.map((link, index) => (
              <span key={link.kind} className="inline-flex items-center gap-1">
                {index > 0 && <span aria-hidden="true">›</span>}
                <button
                  type="button"
                  className="max-w-40 truncate hover:underline"
                  title={link.name}
                  onClick={() => onFilterParent(link, advertiserId)}
                >
                  {link.name}
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
      {incomplete && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              role="img"
              aria-label="数据不完整"
              className="mt-0.5 inline-flex shrink-0 text-amber-500"
            >
              <CircleAlert className="size-3.5" aria-hidden="true" />
            </span>
          </TooltipTrigger>
          <TooltipContent>{incomplete}</TooltipContent>
        </Tooltip>
      )}
      {childDimension(dimension) && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-6 shrink-0"
          aria-label={`查看 ${name} 的下级`}
          onClick={() => onDrill(row)}
        >
          <ChevronRight className="size-4" aria-hidden="true" />
        </Button>
      )}
    </div>
  )
}
