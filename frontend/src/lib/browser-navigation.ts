// 整页跳转（授权页、素材工作台、登录页）统一从这里发出。
// 生产直接交给浏览器；体验版在启动时替换实现，把外部跳转改成站内模拟，避免离开演示环境。
type AssignLocation = (href: string) => void

let assignImpl: AssignLocation = (href) => window.location.assign(href)

export function assignLocation(href: string) {
  assignImpl(href)
}

export function setAssignLocation(impl: AssignLocation) {
  assignImpl = impl
}
