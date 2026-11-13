import { request as httpRequest, createServer } from 'node:http'

// 官方 Web listener 只绑定回环；所有浏览器流量先通过平台管理员校验。
export function createAdminProxy({ apiUrl, upstream, authenticatedUrl, fetchImpl = fetch, recheckMs = 15000 }) {
  const origin = new URL(apiUrl).origin
  const target = new URL(upstream)
  let nativeCookie
  async function authorize(request) {
    if (!request.url.startsWith('/dsh/')) return 404
    const browserOrigin = request.headers.origin
    if (browserOrigin && browserOrigin !== origin) return 403
    if ((!['GET', 'HEAD'].includes(request.method) || request.headers.upgrade) && browserOrigin !== origin) return 403
    try {
      const result = await fetchImpl(`${origin}/api/admin/dsh/authorize`, {
        headers: { cookie: request.headers.cookie || '' }, redirect: 'error', signal: AbortSignal.timeout(5000),
      })
      return result.ok ? 200 : [401, 403].includes(result.status) ? result.status : 503
    } catch { return 503 }
  }
  async function headers(request) {
    // 使用官方令牌换 cookie；不向浏览器、日志或 TK-ADA 转发原生凭据。
    if (!nativeCookie) {
      const result = await fetchImpl(authenticatedUrl(upstream), { redirect: 'manual', signal: AbortSignal.timeout(5000) })
      if (result.status !== 303 || !result.headers.get('set-cookie')) throw new Error('DSH browser authentication unavailable')
      nativeCookie = result.headers.get('set-cookie').split(';')[0]
    }
    const forwarded = { host: target.host, cookie: nativeCookie }
    for (const key of ['accept', 'content-type', 'content-length', 'range', 'upgrade', 'connection', 'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-protocol']) {
      if (request.headers[key]) forwarded[key] = request.headers[key]
    }
    if (request.headers.origin) forwarded.origin = target.origin
    return forwarded
  }
  function deny(response, status) {
    response.writeHead(status, { 'cache-control': 'no-store', 'content-type': 'text/plain' })
    response.end('DSH administrator access required')
  }
  const sockets = new Set()
  const server = createServer(async (request, response) => {
    if (request.url === '/dsh') { response.writeHead(308, { location: '/dsh/', 'cache-control': 'no-store' }); response.end(); return }
    const status = await authorize(request)
    if (status !== 200) { deny(response, status); return }
    try {
      const proxy = httpRequest(new URL(target.origin), { method: request.method, path: request.url.slice('/dsh'.length), headers: await headers(request) }, incoming => {
        const outgoing = { ...incoming.headers, 'cache-control': 'no-store', 'referrer-policy': 'no-referrer' }
        delete outgoing['set-cookie']
        response.writeHead(incoming.statusCode, outgoing)
        incoming.pipe(response)
        incoming.on('error', () => response.destroy())
      })
      proxy.setTimeout(120000, () => proxy.destroy())
      proxy.on('error', () => { if (!response.headersSent) deny(response, 502); else response.destroy() })
      response.on('close', () => proxy.destroy())
      request.pipe(proxy)
    } catch { deny(response, 503) }
  })
  server.on('upgrade', async (request, socket, head) => {
    const reject = status => socket.end(`HTTP/1.1 ${status} Access denied\r\nConnection: close\r\n\r\n`)
    const status = await authorize(request)
    if (status !== 200) { reject(status); return }
    try {
      const proxy = httpRequest(target, { path: request.url.slice('/dsh'.length), headers: await headers(request) })
      proxy.setTimeout(5000, () => proxy.destroy())
      proxy.on('error', () => socket.destroy())
      socket.on('error', () => proxy.destroy())
      proxy.on('response', () => { reject(502); proxy.destroy() })
      proxy.on('upgrade', (response, remote, remoteHead) => {
        proxy.setTimeout(0)
        const lines = Object.entries(response.headers).filter(([key]) => key !== 'set-cookie').map(([key, value]) => `${key}: ${value}`)
        socket.write(`HTTP/1.1 101 Switching Protocols\r\n${lines.join('\r\n')}\r\n\r\n`)
        if (remoteHead.length) socket.write(remoteHead)
        if (head.length) remote.write(head)
        socket.pipe(remote).pipe(socket)
        sockets.add(socket)
        // 已连接的管理会话也定期回查，撤权、禁用或过期时关闭。
        let checking = false
        const timer = setInterval(async () => {
          if (checking) return
          checking = true
          try { if (await authorize(request) !== 200) socket.destroy() } finally { checking = false }
        }, recheckMs)
        timer.unref()
        socket.on('close', () => { clearInterval(timer); sockets.delete(socket); remote.destroy() })
        remote.on('error', () => socket.destroy())
        remote.on('close', () => socket.destroy())
      })
      proxy.end()
    } catch { reject(503) }
  })
  return { server, close: () => { for (const socket of sockets) socket.destroy(); server.closeAllConnections(); return new Promise(resolve => server.close(resolve)) } }
}

export const inject = ['connection', 'webServer']
export function apply(ctx, config) {
  ctx.effect(() => {
    const proxy = createAdminProxy({ apiUrl: config.apiUrl, upstream: 'http://127.0.0.1:9082/', authenticatedUrl: url => ctx.connection.authenticatedUrl(url) })
    proxy.server.listen(9081, '0.0.0.0')
    return proxy.close
  })
}
