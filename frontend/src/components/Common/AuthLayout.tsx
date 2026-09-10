import { WorkspaceBrand } from "./WorkspaceBrand"

export function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-svh flex-col bg-muted">
      <header className="border-b bg-card px-6 py-5 md:px-10">
        <WorkspaceBrand />
      </header>
      <main className="flex flex-1 items-center justify-center px-4 py-12">
        <div className="w-full max-w-[420px]">{children}</div>
      </main>
      <footer className="p-6 text-center text-xs text-muted-foreground">
        TK-ADA
      </footer>
    </div>
  )
}
