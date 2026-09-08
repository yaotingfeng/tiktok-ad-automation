const RETURN_KEY = "workspace-login-return"
const EXPIRED_KEY = "workspace-login-expired"

export function safeReturnPath(value: string | null): string | null {
  if (
    !value?.startsWith("/") ||
    value.startsWith("//") ||
    value.includes("\\") ||
    Array.from(value).some((character) => character.charCodeAt(0) < 32)
  )
    return null
  const target = new URL(value, window.location.origin)
  if (
    target.origin !== window.location.origin ||
    !/^(\/(admin|settings|platform\/tenants)|\/tenants\/[a-f0-9-]{36}\/(accounts|members|builds\/new|build-tasks|materials|strategies|providers))\/?$/i.test(
      target.pathname,
    )
  )
    return null
  return target.pathname + target.search
}
export function rememberLoginReturn(value: string, expired = false) {
  const target = safeReturnPath(value)
  if (target) sessionStorage.setItem(RETURN_KEY, target)
  else sessionStorage.removeItem(RETURN_KEY)
  if (expired) sessionStorage.setItem(EXPIRED_KEY, "true")
}
export function consumeLoginReturn() {
  const target = safeReturnPath(sessionStorage.getItem(RETURN_KEY))
  clearLoginReturn()
  return target
}
export function clearLoginReturn() {
  sessionStorage.removeItem(RETURN_KEY)
  sessionStorage.removeItem(EXPIRED_KEY)
}
export function loginExpired() {
  return sessionStorage.getItem(EXPIRED_KEY) === "true"
}
