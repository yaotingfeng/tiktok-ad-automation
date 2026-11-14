import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer, request as httpRequest } from 'node:http'
import { test } from 'node:test'
import { createAdminProxy } from './admin-proxy.mjs'

async function listen(server) { server.listen(0, '127.0.0.1'); await once(server, 'listening'); return `http://127.0.0.1:${server.address().port}` }

test('gateway checks every request, strips credentials/prefix and rejects cross-origin writes', async () => {
  const seen = [], auth = []
  let allowed = true, exchange = 0
  const upstream = createServer((request, response) => {
    seen.push({ url: request.url, headers: request.headers })
    response.writeHead(200, { 'set-cookie': 'private-native-cookie=hidden', 'content-type': 'text/html' })
    response.end('official-ui')
  })
  const target = await listen(upstream)
  const proxy = createAdminProxy({ apiUrl: 'https://app.example', upstream: target, authenticatedUrl: () => target + '/?token=private', fetchImpl: async (url, options) => {
    if (url.includes('?token=')) { exchange++; return new Response(null, { status: 303, headers: { 'set-cookie': 'native=private; HttpOnly' } }) }
    auth.push(options.headers)
    if (options.headers.cookie === 'unavailable') throw new Error('offline')
    return new Response(null, { status: allowed && options.headers.cookie === 'admin=yes' ? 204 : 403 })
  } })
  const gateway = await listen(proxy.server)
  try {
    assert.equal((await fetch(gateway + '/dsh/')).status, 403)
    const result = await fetch(gateway + '/dsh/plugins/model/client.js?q=1', { headers: { cookie: 'admin=yes', authorization: 'Bearer should-not-leak' } })
    assert.equal(await result.text(), 'official-ui')
    assert.equal(result.headers.get('set-cookie'), null)
    assert.equal(result.headers.get('cache-control'), 'no-store')
    assert.equal(seen[0].url, '/plugins/model/client.js?q=1')
    assert.equal(seen[0].headers.cookie, 'native=private')
    assert.equal(seen[0].headers.authorization, undefined)
    assert.equal(exchange, 1)
    for (const origin of ['https://evil.example', undefined]) {
      assert.equal((await fetch(gateway + '/dsh/api/settings', { method: 'POST', headers: { cookie: 'admin=yes', ...(origin ? { origin } : {}) } })).status, 403)
    }
    assert.equal((await fetch(gateway + '/dsh/api/settings', { method: 'POST', headers: { cookie: 'admin=yes', origin: 'https://app.example' }, body: '{}' })).status, 200)
    assert.equal(seen[1].headers.origin, target)
    allowed = false
    assert.equal((await fetch(gateway + '/dsh/', { headers: { cookie: 'admin=yes' } })).status, 403)
    assert.equal((await fetch(gateway + '/dsh/', { headers: { cookie: 'unavailable' } })).status, 503)
    assert.equal(seen.length, 2)
    assert.ok(auth.length >= 4)
  } finally { await proxy.close(); upstream.closeAllConnections(); await new Promise(resolve => upstream.close(resolve)) }
})

test('WebSocket handshake is gated and live connections close after revocation', { timeout: 5000 }, async () => {
  let allowed = true, upgradeCount = 0
  const upstreamSockets = new Set()
  const upstream = createServer()
  upstream.on('upgrade', (request, socket) => {
    upgradeCount++
    upstreamSockets.add(socket)
    socket.on('error', () => {})
    socket.on('close', () => upstreamSockets.delete(socket))
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
  })
  const target = await listen(upstream)
  const proxy = createAdminProxy({ apiUrl: 'https://app.example', upstream: target, authenticatedUrl: () => target + '?token=x', recheckMs: 25, fetchImpl: async url => url.includes('?token=') ? new Response(null, { status: 303, headers: { 'set-cookie': 'native=x' } }) : new Response(null, { status: allowed ? 204 : 403 }) })
  const gateway = await listen(proxy.server)
  function upgrade(origin) {
    return httpRequest(gateway + '/dsh/api/events', { headers: { upgrade: 'websocket', connection: 'Upgrade', origin } })
  }
  try {
    const rejected = upgrade('https://evil.example')
    const rejectedResponse = once(rejected, 'response'); rejected.end()
    assert.equal((await rejectedResponse)[0].statusCode, 403)
    assert.equal(upgradeCount, 0)
    const accepted = upgrade('https://app.example')
    const upgraded = once(accepted, 'upgrade')
    accepted.end()
    const [, socket] = await upgraded
    socket.on('error', () => {})
    assert.equal(upgradeCount, 1)
    socket.resume()
    allowed = false
    await once(socket, 'close')
  } finally {
    for (const socket of upstreamSockets) socket.destroy()
    await proxy.close()
    upstream.closeAllConnections()
    await new Promise(resolve => upstream.close(resolve))
  }
})
