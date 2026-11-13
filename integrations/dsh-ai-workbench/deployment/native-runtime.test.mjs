import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { cp, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'

const packageRoot = process.env.DSH_TEST_PACKAGE_ROOT

test('production entrypoint boots real DSH, polls TK-ADA and serves readiness without model calls', { skip: !packageRoot, timeout: 45000 }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'tkada-dsh-production-'))
  const requests = []
  const api = createServer(async (request, response) => {
    let body = ''
    for await (const chunk of request) body += chunk
    requests.push({ path: request.url, authorization: request.headers.authorization, body: JSON.parse(body || '{}') })
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ task: null }))
  })
  let child, output = ''
  try {
    api.listen(0, '127.0.0.1')
    await once(api, 'listening')
    const portReservation = createServer()
    portReservation.listen(0, '127.0.0.1')
    await once(portReservation, 'listening')
    const healthPort = portReservation.address().port
    await new Promise(resolve => portReservation.close(resolve))
    await cp(dirname(dirname(fileURLToPath(import.meta.url))), join(root, 'plugin'), { recursive: true })
    await symlink(join(resolve(packageRoot), 'node_modules'), join(root, 'node_modules'), 'dir')
    const token = 'test-only-private-service-credential'
    await writeFile(join(root, 'token'), token, { mode: 0o600 })
    await writeFile(join(root, 'provider.json'), JSON.stringify({ provider: 'openai', model: 'gpt-4o-mini', config: { apiKeyEnv: 'DSH_SMOKE_MODEL_KEY', baseURL: `http://127.0.0.1:${api.address().port}/model` } }))
    const require = createRequire(join(resolve(packageRoot), 'package.json'))
    // 全新 HOME 和最小环境，避免读取开发者的个人模型密钥或 DSH 会话。
    child = spawn(process.execPath, [join(root, 'plugin/deployment/start.mjs')], { cwd: root, env: {
      PATH: process.env.PATH, HOME: root, DSH_HOME: join(root, 'home'), DSH_WORKSPACE: join(root, 'workspace'),
      DSH_PROVIDER_FILE: join(root, 'provider.json'), AI_DSH_TOKEN_FILE: join(root, 'token'),
      TKADA_API_URL: `http://127.0.0.1:${api.address().port}`, PORT: String(healthPort),
      DSH_CLI: require.resolve('@deepseek-ai/dsh/lib/bin.js'), DSH_SMOKE_MODEL_KEY: 'test-only-unused-model-key', DSH_TELEMETRY_DISABLED: '1',
    }, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
    child.stdout.on('data', chunk => { output += chunk })
    child.stderr.on('data', chunk => { output += chunk })
    let ready = false
    for (let attempt = 0; attempt < 150; attempt++) {
      if (child.exitCode !== null) break
      try {
        const response = await fetch(`http://127.0.0.1:${healthPort}/ready`)
        ready = response.ok && (await response.json()).ready === true
      } catch {}
      if (ready && requests.length) break
      await delay(100)
    }
    assert.ok(ready, `DSH readiness failed:\n${output}`)
    assert.ok(requests.length, `DSH did not poll TK-ADA:\n${output}`)
    assert.ok(requests.every(request => request.path === '/api/integrations/ai-workbench/reviews/claim'))
    assert.equal(requests[0].authorization, `Bearer ${token}`)
    assert.equal(requests[0].body.main.key, 'ai-delivery-review')
    const patch = await readFile(join(root, 'home/profiles/tkada/cordis.patch.yml'), 'utf8')
    assert.ok(!patch.includes('test-only-unused-model-key'))
    assert.equal((await fetch(`http://127.0.0.1:${healthPort}/sessions`)).status, 404)
    child.kill('SIGTERM')
    const stopped = await Promise.race([once(child, 'exit').then(([code]) => code), delay(10000, undefined, { ref: false }).then(() => 'timeout')])
    assert.equal(stopped, 0, `DSH failed graceful shutdown:\n${output}`)
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) { process.kill(-child.pid, 'SIGKILL'); await once(child, 'exit') }
    api.closeAllConnections()
    await new Promise(resolve => api.close(resolve))
    await rm(root, { recursive: true, force: true })
  }
})
