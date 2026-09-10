import { createContext, type ReactNode, useContext } from "react"
import { createPortal } from "react-dom"

// Keep the title owned by its page so dynamic draft/task names and error states
// follow the same route lifecycle. Standalone pages still render their own h1.
export const WorkspaceTitleTarget = createContext<HTMLElement | null>(null)

export function WorkspacePageTitle({ children }: { children: ReactNode }) {
  const target = useContext(WorkspaceTitleTarget)
  const title = <h1 className="truncate text-base font-medium">{children}</h1>
  return target ? createPortal(title, target) : title
}
