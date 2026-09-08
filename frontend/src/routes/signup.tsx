import { createFileRoute, redirect } from "@tanstack/react-router"
import { isLoggedIn } from "@/hooks/useAuth"

// Existing bookmarks have a safe destination; account creation is administrator-only.
export const Route = createFileRoute("/signup")({
  beforeLoad: () => {
    throw redirect({ to: isLoggedIn() ? "/" : "/login" })
  },
})
