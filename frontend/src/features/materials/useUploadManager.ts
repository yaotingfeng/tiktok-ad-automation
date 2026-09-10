import { useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useState, useSyncExternalStore } from "react"
import { getApiFeedback, subscribeApiFeedback } from "@/lib/api-feedback"
import { IngestManager } from "./ingest-manager"

export function useUploadManager(tenantId: string, bcId: string) {
  const [control] = useState(() => new IngestManager(tenantId, bcId))
  const queryClient = useQueryClient()
  const state = useSyncExternalStore(control.subscribe, control.getSnapshot)
  useEffect(() => {
    control.activate()
    // Logout clears the query cache synchronously, before route teardown. Cross-tab
    // storage changes and any current permission failure also stop active direct PUTs.
    const checkSession = control.checkAuthentication
    const unsubscribe = queryClient.getQueryCache().subscribe(checkSession)
    const permissions = subscribeApiFeedback(() => {
      if (getApiFeedback()) control.revokePermission()
      else checkSession()
    })
    window.addEventListener("storage", checkSession)
    return () => {
      unsubscribe()
      permissions()
      window.removeEventListener("storage", checkSession)
      control.dispose()
    }
  }, [control, queryClient])
  return {
    ...state,
    control,
    start: control.start,
    recover: control.recover,
    retryCreation: control.retryCreation,
    restorePendingFiles: control.restorePendingFiles,
    resume: control.resume,
    retry: control.retry,
    revokePermission: control.revokePermission,
  }
}
export type UploadManager = ReturnType<typeof useUploadManager>
export function useFileProgress(manager: UploadManager, clientIndex: number) {
  const subscribe = useCallback(
    (listener: () => void) =>
      manager.control.subscribeProgress(clientIndex, listener),
    [manager.control, clientIndex],
  )
  const get = useCallback(
    () => manager.control.getProgress(clientIndex),
    [manager.control, clientIndex],
  )
  return useSyncExternalStore(subscribe, get)
}
