import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { ProvidersService, type ResolvedLink } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ProviderError, safeError } from "./presentation"
import { providerKey } from "./queries"
export function CandidateDialog({
  tenantId,
  item,
  onClose,
}: {
  tenantId: string
  item: ResolvedLink
  onClose: () => void
}) {
  const [pending, setPending] = useState(false),
    [error, setError] = useState<string>()
  const client = useQueryClient()
  const choose = async (externalId: string) => {
    if (pending) return
    setPending(true)
    setError(undefined)
    try {
      await ProvidersService.postCandidate({
        path: { tenant_id: tenantId, input_id: item.input_id },
        body: { external_drama_id: externalId },
      })
      await client.invalidateQueries({ queryKey: providerKey(tenantId) })
      onClose()
    } catch (e) {
      setError(safeError(e))
      if (safeError(e) === "action_forbidden")
        void client.invalidateQueries({
          queryKey: ["tenant", tenantId, "scope"],
        })
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !pending) onClose()
      }}
    >
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>选择对应剧目</DialogTitle>
          <DialogDescription>
            第 {item.line_no} 行：{item.raw_input}
            。仅继续处理这一行，其余结果保持原状。
          </DialogDescription>
        </DialogHeader>
        {error && <ProviderError error={error} />}
        <div className="overflow-hidden rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>正式剧名</TableHead>
                <TableHead>语言</TableHead>
                <TableHead>应用 / 剧目 ID</TableHead>
                <TableHead>操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {item.candidates?.map((candidate) => (
                <TableRow key={candidate.external_drama_id}>
                  <TableCell>{candidate.title}</TableCell>
                  <TableCell>{candidate.language || "待核实"}</TableCell>
                  <TableCell>
                    <p>{item.application_id}</p>
                    <p>{candidate.external_drama_id}</p>
                  </TableCell>
                  <TableCell>
                    <Button
                      disabled={pending}
                      onClick={() => void choose(candidate.external_drama_id)}
                    >
                      使用此剧目
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </DialogContent>
    </Dialog>
  )
}
