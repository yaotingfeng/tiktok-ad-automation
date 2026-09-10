export function WorkspaceBrand() {
  return (
    <div className="flex items-center gap-2.5">
      <img
        src="/assets/brand/icon-192.png"
        alt="TK-ADA 图标"
        width={32}
        height={32}
        className="size-8 shrink-0 rounded-lg"
      />
      <div className="brand-copy min-w-0">
        <p className="text-lg leading-6 font-semibold">TK-ADA</p>
        <p className="text-[11px] leading-4 text-muted-foreground">
          广告投放工具
        </p>
      </div>
    </div>
  )
}
