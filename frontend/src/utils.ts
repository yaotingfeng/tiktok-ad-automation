import { AxiosError } from "axios"
import { PERMISSION_MESSAGE } from "@/lib/api-feedback"

function extractErrorMessage(err: Error): string {
  if (err instanceof AxiosError) {
    if (err.response?.status === 403) return PERMISSION_MESSAGE
    if (err.response?.status === 401) return "邮箱或密码不正确，或登录已过期。"
    const errDetail = (err.response?.data as any)?.detail
    if (Array.isArray(errDetail) && errDetail.length > 0) {
      return errDetail[0].msg
    }
    if (typeof errDetail === "string") {
      return errDetail
    }
    return err.message
  }
  return "操作未完成，请稍后重试。"
}

export const handleError = function (this: (msg: string) => void, err: Error) {
  const errorMessage = extractErrorMessage(err)
  this(errorMessage)
}

export const getInitials = (name: string): string => {
  return name
    .split(" ")
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase()
}
