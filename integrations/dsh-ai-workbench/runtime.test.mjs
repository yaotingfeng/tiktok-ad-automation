import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { loadMethods, skillName } from './methods.mjs'
import { createRuntime, runClaim, COMPLETE_TOOL } from './runtime.mjs'

test('service credential is used only for claim, task completion uses scoped token', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-review-'))
  try {
    const tokenFile = join(directory, 'service-token')
    await writeFile(tokenFile, 'private-service-key', { mode: 0o600 })
    const requests = []
    const runtime = createRuntime({ url: 'https://tkada.example', tokenFile, stateRoot: directory }, async (url, options) => {
      requests.push({ url, options })
      return { ok: true, json: async () => ({ status: 'COMPLETE' }) }
    })
    await runtime.claim()
    await runtime.complete({ task: { id: 'task-1' }, task_token: 'short-task-key' }, { summary: 'no change', proposals: [] })
    await runtime.searchKnowledge({ task: { id: 'task-1' }, task_token: 'short-task-key' }, { query: '赔付' })
    await runtime.readKnowledge({ task: { id: 'task-1' }, task_token: 'short-task-key' }, { knowledge_id: 'knowledge-1' })
    assert.equal(requests[2].options.headers.authorization, 'Bearer short-task-key')
    assert.equal(requests[3].options.headers.authorization, 'Bearer short-task-key')
    assert.ok(requests[2].url.endsWith('/task-1/knowledge/search'))
    assert.ok(requests[3].url.endsWith('/task-1/knowledge/read'))
    assert.equal(requests[0].options.headers.authorization, 'Bearer private-service-key')
    assert.equal(requests[1].options.headers.authorization, 'Bearer short-task-key')
    assert.equal(requests[1].options.redirect, 'error')
    assert.throws(() => createRuntime({ url: 'http://outside.example', tokenFile, stateRoot: directory }), /HTTPS/)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('native DSH session has only structured result tool and a tenant/task directory', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-dsh-test-'))
  try {
    const claim = { task: { id: 'task-1', tenant_id: 'tenant-a', optimizer_id: 'optimizer-a', fence: 'fence-1', expires_at: new Date(Date.now() + 60000).toISOString(), evidence: { optimizer: { id: 'optimizer-a' }, methods: await loadMethods() } }, task_token: 'short-token' }
    let registered, allowed, guard, prompt, options, disposed = false
    const tools = new Map(), skills = new Map()
    const ctx = { get: () => ({ currentSelection: () => ({ provider: 'test', model: 'test' }) }), agentPresets: { mount: async () => {} }, agents: { create: async input => {
      options = input
      const agent = { followup: message => { prompt = message }, whenIdle: async () => { await assert.rejects(registered.execute({ summary: 'premature', proposals: [] }), /Load the main/); await tools.get('skill').execute({ name: skillName('ai-delivery-review') }); await tools.get('skill').execute({ name: skillName('scale') }); assert.equal((await tools.get('tkada_search_knowledge').execute({ query: '补贴' })).items[0].id, 'knowledge-1'); assert.equal((await tools.get('tkada_read_knowledge').execute({ knowledge_id: 'knowledge-1' })).body, 'policy text'); await registered.execute({ summary: 'insufficient evidence', proposals: [] }) } }
      await input.setup({ skills: { register: s => skills.set(s.name, s), get: async name => skills.get(name) }, tools: { restrict: value => { allowed = value.allow }, guard: value => { guard = value }, register: value => { tools.set(value.name, value); if (value.name === COMPLETE_TOOL) registered = value } } }, agent)
      return { agent, dispose: async () => { disposed = true } }
    } } }
    const completions = []
    await runClaim(ctx, { stateRoot: directory }, claim, { searchKnowledge: async () => ({ items: [{ id: 'knowledge-1' }] }), readKnowledge: async () => ({ id: 'knowledge-1', body: 'policy text' }), complete: async (identity, result) => { completions.push({ identity, result }); return { status: 'COMPLETE' } } }, { SessionId: value => value, createUserMessage: value => value, renderSkillContent: s => s.content, scopeOf: () => "task-scope" })
    assert.deepEqual(allowed, [])
    assert.deepEqual([...tools.keys()].sort(), ["skill", "tkada_search_knowledge", "tkada_read_knowledge", "tkada_backtest_rule", COMPLETE_TOOL].sort())
    assert.equal(guard({ name: 'exec' }), 'Only task skills, scoped knowledge and structured review completion are permitted')
    assert.ok(options.meta.cwd.includes('tenant-a/optimizer-a/task-1/fence-1'))
    assert.ok(!JSON.stringify(prompt).includes('short-token'))
    assert.equal(completions.length, 1)
    assert.deepEqual(completions[0].result.method_usage, ['ai-delivery-review', 'scale'])
    assert.ok(!JSON.stringify(prompt).includes('必需：完整当地日'))
    assert.equal(skills.size, 6)
    assert.equal(disposed, true)
  } finally { await rm(directory, { recursive: true, force: true }) }
})
