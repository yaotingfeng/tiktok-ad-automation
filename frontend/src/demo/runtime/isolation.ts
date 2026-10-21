import { reportDemoGap } from "./gaps"

// 体验版与生产版同域：先换掉浏览器存储与网络出口，再加载任何应用代码。
// 1. localStorage / sessionStorage 换成内存实现：读不到生产登录凭证，也不会写入生产会用到的键。
// 2. IndexedDB 数据库名统一加体验版前缀：素材上传的断点记录不会读到或覆盖生产版同名库；
//    每次启动先清空体验版库，刷新即回到初始状态。
// 3. fetch / XMLHttpRequest 只放行体验版注册的模拟地址，其余一律拒绝并提示。

function createMemoryStorage(): Storage {
  const store = {} as Storage & Record<string, string>
  const hidden = (name: string, value: unknown) =>
    Object.defineProperty(store, name, { value, enumerable: false })
  hidden("getItem", (key: string) =>
    Object.keys(store).includes(key) ? store[key] : null,
  )
  hidden("setItem", (key: string, value: string) => {
    store[key] = String(value)
  })
  hidden("removeItem", (key: string) => {
    delete store[key]
  })
  hidden("clear", () => {
    for (const key of Object.keys(store)) delete store[key]
  })
  hidden("key", (index: number) => Object.keys(store)[index] ?? null)
  Object.defineProperty(store, "length", {
    get: () => Object.keys(store).length,
    enumerable: false,
  })
  return store
}

export type DemoFetchHandler = (
  url: URL,
  init: RequestInit | undefined,
) => Promise<Response> | Response

const fetchHandlers: { prefix: string; handler: DemoFetchHandler }[] = []

/** 素材直传等绕过生成客户端的请求，由各领域按地址前缀注册模拟处理。 */
export function registerDemoFetch(prefix: string, handler: DemoFetchHandler) {
  fetchHandlers.push({ prefix, handler })
}

const IDB_PREFIX = "tk-ada-demo:"

function isolateIndexedDB() {
  const factory = window.indexedDB
  if (!factory) return Promise.resolve()
  const open = factory.open.bind(factory)
  const remove = factory.deleteDatabase.bind(factory)
  const list = factory.databases?.bind(factory)
  const cleanup = list
    ? list()
        .then((items) =>
          Promise.all(
            items
              .filter((item) => item.name?.startsWith(IDB_PREFIX))
              .map(
                (item) =>
                  new Promise<void>((resolve) => {
                    const request = remove(item.name as string)
                    request.onsuccess = () => resolve()
                    request.onerror = () => resolve()
                    request.onblocked = () => resolve()
                  }),
              ),
          ),
        )
        .then(() => undefined)
        .catch(() => undefined)
    : Promise.resolve()
  factory.open = ((name: string, version?: number) =>
    version === undefined
      ? open(`${IDB_PREFIX}${name}`)
      : open(`${IDB_PREFIX}${name}`, version)) as typeof factory.open
  factory.deleteDatabase = ((name: string) =>
    remove(`${IDB_PREFIX}${name}`)) as typeof factory.deleteDatabase
  if (list)
    factory.databases = async () =>
      (await list())
        .filter((item) => item.name?.startsWith(IDB_PREFIX))
        .map((item) => ({ ...item, name: item.name?.slice(IDB_PREFIX.length) }))
  return cleanup
}

/** 返回的 Promise 在体验版旧数据清理完成后结束；应用代码要等它完成再加载。 */
export function installDemoIsolation(): Promise<void> {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    Object.defineProperty(window, name, {
      value: createMemoryStorage(),
      configurable: true,
      enumerable: true,
      writable: false,
    })
  }

  const blockedFetch: typeof fetch = async (input, init) => {
    const href =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url
    const url = new URL(href, window.location.href)
    const match = fetchHandlers.find(({ prefix }) =>
      url.href.startsWith(prefix),
    )
    if (match) return match.handler(url, init)
    // 同源静态资源（打包产物、字体）不属于业务请求，允许读取；/api 一律拒绝。
    if (
      url.origin === window.location.origin &&
      url.pathname.startsWith("/demo/") &&
      !url.pathname.startsWith("/api")
    )
      return nativeFetch(input, init)
    reportDemoGap(`fetch ${url.origin}${url.pathname}`)
    throw new TypeError("体验版不发送真实网络请求")
  }
  const nativeFetch = window.fetch.bind(window)
  window.fetch = blockedFetch

  XMLHttpRequest.prototype.open = function blockedOpen(
    _method: string,
    url: string | URL,
  ) {
    reportDemoGap(`xhr ${String(url)}`)
    throw new TypeError("体验版不发送真实网络请求")
  } as typeof XMLHttpRequest.prototype.open

  if (navigator.sendBeacon)
    navigator.sendBeacon = (url: string | URL) => {
      reportDemoGap(`beacon ${String(url)}`)
      return false
    }

  return isolateIndexedDB()
}
