// 列表分页的通用实现：支持 after_id/cursor 游标和 offset 两种写法，返回 { items, next_cursor, total }。
export function paginate<T>(
  items: T[],
  query: {
    after_id?: string | null
    cursor?: string | null
    limit?: number | null
    offset?: number | null
  } = {},
  idOf: (item: T) => string = (item) =>
    String((item as { id?: string }).id ?? ""),
) {
  const limit = Number(query.limit ?? 50) || 50
  const cursor = query.after_id ?? query.cursor ?? null
  let start = Number(query.offset ?? 0) || 0
  if (cursor) {
    const index = items.findIndex((item) => idOf(item) === cursor)
    start = index >= 0 ? index + 1 : 0
  }
  const page = items.slice(start, start + limit)
  const hasMore = start + limit < items.length
  return {
    items: page,
    next_cursor: hasMore && page.length ? idOf(page[page.length - 1]) : null,
    total: items.length,
  }
}

/** 简单的关键词过滤：任一字段包含关键词即命中（忽略大小写）。 */
export function matchesSearch(
  search: string | null | undefined,
  ...fields: (string | null | undefined)[]
) {
  if (!search?.trim()) return true
  const needle = search.trim().toLowerCase()
  return fields.some((field) => field?.toLowerCase().includes(needle))
}
