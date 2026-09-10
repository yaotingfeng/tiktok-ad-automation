import type { ReactNode } from "react"

// Dynamic names and access states keep their title within the page lifecycle.
export function WorkspacePageTitle({ children }: { children: ReactNode }) {
  return (
    <h1 className="text-[22px] leading-8 font-semibold tracking-tight">
      {children}
    </h1>
  )
}
