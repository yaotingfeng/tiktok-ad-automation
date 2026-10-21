import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query"
import {
  createRouter,
  type RouterHistory,
  RouterProvider,
} from "@tanstack/react-router"
import { type ReactNode, StrictMode } from "react"
import ReactDOM from "react-dom/client"
import { ThemeProvider } from "./components/theme-provider"
import { Toaster } from "./components/ui/sonner"
import { handleApiError } from "./lib/api-feedback"
import "./index.css"
import {
  parseWorkspaceSearch,
  stringifyWorkspaceSearch,
} from "./lib/search-params"
import { routeTree } from "./routeTree.gen"

// 生产入口与体验版入口共用同一套应用装配；体验版只替换路由历史并在外层叠加演示外壳。
export function createAppQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (count, error) =>
          ![401, 403].includes(
            (error as { response?: { status: number } }).response?.status ?? 0,
          ) && count < 2,
      },
    },
    queryCache: new QueryCache({
      onError: handleApiError,
    }),
    mutationCache: new MutationCache({
      onError: handleApiError,
    }),
  })
}

export function createAppRouter(history?: RouterHistory) {
  return createRouter({
    routeTree,
    history,
    parseSearch: parseWorkspaceSearch,
    stringifySearch: stringifyWorkspaceSearch,
  })
}

export type AppRouter = ReturnType<typeof createAppRouter>

declare module "@tanstack/react-router" {
  interface Register {
    router: AppRouter
  }
}

export function renderApp({
  router,
  queryClient,
  overlay,
}: {
  router: AppRouter
  queryClient: QueryClient
  overlay?: ReactNode
}) {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <ThemeProvider defaultTheme="light" storageKey="workbench-theme">
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
          {overlay}
          <Toaster richColors closeButton />
        </QueryClientProvider>
      </ThemeProvider>
    </StrictMode>,
  )
}
