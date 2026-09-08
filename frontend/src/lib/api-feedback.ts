import { AxiosError } from "axios"
import { rememberLoginReturn } from "./login-return"

export const PERMISSION_MESSAGE =
  "你没有执行此操作的权限。请联系管理员检查角色和租户权限。"
let permissionMessage: string | null = null
const listeners = new Set<() => void>()

export const subscribeApiFeedback = (listener: () => void) => {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
export const getApiFeedback = () => permissionMessage
export const clearApiFeedback = () => {
  permissionMessage = null
  listeners.forEach((listener) => {
    listener()
  })
}

// Both query and mutation failures share this policy. 403 never removes the session.
export function handleApiError(error: Error) {
  if (!(error instanceof AxiosError)) return
  if (error.response?.status === 401) {
    localStorage.removeItem("access_token")
    if (window.location.pathname !== "/login") {
      rememberLoginReturn(
        window.location.pathname + window.location.search,
        true,
      )
      window.location.assign("/login")
    }
  } else if (error.response?.status === 403) {
    permissionMessage = PERMISSION_MESSAGE
    listeners.forEach((listener) => {
      listener()
    })
  }
}
