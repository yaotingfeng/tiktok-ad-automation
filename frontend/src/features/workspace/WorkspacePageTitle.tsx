import { createContext, type ReactNode, useContext } from "react"
import { createPortal } from "react-dom"

// Keep the title owned by its page so dynamic draft/task names and error states
// follow the same route lifecycle. Standalone pages still render their own h1.
export const WorkspaceTitleTarget = createContext<HTMLElement | null>(null)

export function WorkspacePageTitle({
  children,
  placement = "header",
  sectionLabel,
}: {
  children: ReactNode
  placement?: "header" | "content"
  sectionLabel?: string
}) {
  const target = useContext(WorkspaceTitleTarget)
  if (placement === "content") {
    return (
      <>
        {target &&
          sectionLabel &&
          createPortal(
            <span className="text-muted-foreground">{sectionLabel}</span>,
            target,
          )}
        <h1 className="text-[22px] leading-8 font-semibold tracking-tight">
          {children}
        </h1>
      </>
    )
  }
  const title = <h1 className="truncate text-base font-medium">{children}</h1>
  return target ? createPortal(title, target) : title
}
