import { ShieldAlert } from "lucide-react"
import { useSyncExternalStore } from "react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  clearApiFeedback,
  getApiFeedback,
  subscribeApiFeedback,
} from "@/lib/api-feedback"

export function ApiFeedback() {
  const message = useSyncExternalStore(subscribeApiFeedback, getApiFeedback)
  if (!message) return null
  return (
    <Alert variant="destructive">
      <ShieldAlert />
      <AlertTitle>无操作权限</AlertTitle>
      <AlertDescription>
        <p>{message}</p>
        <Button variant="outline" size="sm" onClick={clearApiFeedback}>
          知道了
        </Button>
      </AlertDescription>
    </Alert>
  )
}
