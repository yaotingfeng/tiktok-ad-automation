import { useState } from "react"

// 签名游标属于一个草稿版本/代次；渲染新版本时同步归零，不能先发出旧游标请求。
export function useImageCursorPage(scope: string) {
  const [state, setState] = useState({
    scope,
    cursors: [null] as Array<string | null>,
    limit: 50,
  })
  const current =
    state.scope === scope ? state : { ...state, scope, cursors: [null] }
  if (state.scope !== scope) setState(current)
  return {
    cursor: current.cursors[current.cursors.length - 1] ?? null,
    page: current.cursors.length,
    limit: current.limit,
    reset: () => setState((s) => ({ ...s, cursors: [null] })),
    setLimit: (limit: number) =>
      setState((s) => ({ ...s, limit, cursors: [null] })),
    previous: () =>
      setState((s) => ({ ...s, cursors: s.cursors.slice(0, -1) })),
    next: (cursor: string) =>
      setState((s) => ({ ...s, cursors: [...s.cursors, cursor] })),
  }
}
export interface ImageEditorState {
  ids: string[]
  revision: number | undefined
}
