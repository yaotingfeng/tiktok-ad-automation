import { createFileRoute, Outlet, redirect } from "@tanstack/react-router"
import { ApiFeedback } from "@/features/workspace/ApiFeedback"
import { WorkspaceShell } from "@/features/workspace/WorkspaceShell"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: () => {
    if (!isLoggedIn()) throw redirect({ to: "/login" })
  },
})
function Layout() {
  const { user, error, isPending } = useAuth()
  return (
    <WorkspaceShell user={user}>
      <ApiFeedback />
      {isPending ? (
        <p role="status">正在载入工作空间…</p>
      ) : error ? (
        <p role="status">暂时无法读取用户信息，请刷新页面重试。</p>
      ) : (
        <Outlet />
      )}
    </WorkspaceShell>
  )
}
