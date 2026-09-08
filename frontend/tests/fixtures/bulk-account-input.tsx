// Test-only host: renders the exported production component with its public props.
// The parent owns resolution and current-page slicing; no application route is added.
import { useState } from "react"
import { createRoot } from "react-dom/client"
import { AccountsService, type ResolvedLine } from "@/client"
import { client } from "@/client/client.gen"
import { BulkAccountInput } from "@/features/accounts/BulkAccountInput"
import "@/index.css"

client.setConfig({ baseURL: "", auth: () => "bulk-component-boundary-token" })
function Host() {
  const [value, setValue] = useState("")
  const [rows, setRows] = useState<ResolvedLine[]>([])
  const [page, setPage] = useState(0)
  return (
    <main className="p-6">
      <BulkAccountInput
        value={value}
        onChange={setValue}
        rows={rows.slice(page * 2, (page + 1) * 2)}
        onResolve={() => {
          void AccountsService.postResolve({
            path: { tenant_id: "11111111-1111-4111-8111-111111111111" },
            body: {
              bc_id: "7000000000000000001",
              lines: value
                .split("\n")
                .map((raw, i) => ({ raw, line_no: i + 1 })),
            },
          }).then(({ data }) => {
            setRows(data)
            setPage(0)
          })
        }}
      />
      <button type="button" onClick={() => setPage(page + 1)}>
        下一段结果
      </button>
    </main>
  )
}
createRoot(document.getElementById("root")!).render(<Host />)
