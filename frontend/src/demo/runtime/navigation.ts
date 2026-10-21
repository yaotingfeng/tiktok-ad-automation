import { setAssignLocation } from "@/lib/browser-navigation"
import { reportDemoGap } from "./gaps"

// 体验版里的整页跳转：站内路径改走 hash 路由；TikTok 授权、素材工作台等外部地址
// 交给各领域注册的模拟回调，回调返回应跳回的站内路径（例如带授权成功参数的账户页）。
type ExternalRedirect = (url: URL) => string | Promise<string>

const externalRedirects: {
  match: (url: URL) => boolean
  to: ExternalRedirect
}[] = []

export function registerDemoRedirect(
  match: (url: URL) => boolean,
  to: ExternalRedirect,
) {
  externalRedirects.push({ match, to })
}

/**
 * 体验版内应用路由的站内路径（不含 /demo 前缀与 #）。
 * leavePage：真实系统在这里是整页跳转（离开当前页面），页面上的离开确认不会出现，
 * 体验版模拟时也要跳过路由的离开拦截。
 */
export type DemoNavigate = (
  path: string,
  options?: { leavePage?: boolean },
) => void

export function installDemoNavigation(navigate: DemoNavigate) {
  setAssignLocation(async (href) => {
    const url = new URL(href, window.location.href)
    if (url.origin === window.location.origin) {
      // 站内路径可能来自 hash（#/x）或普通路径（/login），都转成路由路径。
      const internal = url.hash.startsWith("#/")
        ? url.hash.slice(1)
        : `${url.pathname.replace(/^\/demo(?=\/|$)/, "") || "/"}${url.search}`
      navigate(internal, { leavePage: true })
      return
    }
    const redirect = externalRedirects.find(({ match }) => match(url))
    if (!redirect) {
      reportDemoGap(`跳转 ${url.origin}${url.pathname}`)
      return
    }
    navigate(await redirect.to(url), { leavePage: true })
  })
}
