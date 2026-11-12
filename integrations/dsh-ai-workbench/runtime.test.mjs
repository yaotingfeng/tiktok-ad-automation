import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
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
    assert.equal(requests[0].options.headers.authorization, 'Bearer private-service-key')
    assert.equal(requests[1].options.headers.authorization, 'Bearer short-task-key')
    assert.equal(requests[1].options.redirect, 'error')
    assert.throws(() => createRuntime({ url: 'http://outside.example', tokenFile, stateRoot: directory }), /HTTPS/)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('native DSH session has only structured result tool and a tenant/task directory', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tkada-dsh-test-'))
  try {
    const claim = { task: { id: 'task-1', tenant_id: 'tenant-a', optimizer_id: 'optimizer-a', fence: 'fence-1', expires_at: new Date(Date.now() + 60000).toISOString(), evidence: { optimizer: { id: 'optimizer-a' } } }, task_token: 'short-token' }
    let registered, allowed, guard, prompt, options, disposed = false
    const ctx = { get: () => ({ currentSelection: () => ({ provider: 'test', model: 'test' }) }), agentPresets: { mount: async () => {} }, agents: { create: async input => {
      options = input
      const agent = { followup: message => { prompt = message }, whenIdle: async () => { await registered.execute({ summary: 'insufficient evidence', proposals: [] }) } }
      await input.setup({ tools: { restrict: value => { allowed = value.allow }, guard: value => { guard = value }, register: value => { registered = value } } }, agent)
      return { agent, dispose: async () => { disposed = true } }
    } } }
    const completions = []
    await runClaim(ctx, { stateRoot: directory }, claim, { complete: async (identity, result) => { completions.push({ identity, result }); return { status: 'COMPLETE' } } }, { SessionId: value => value, createUserMessage: value => value })
    assert.deepEqual(allowed, [COMPLETE_TOOL])
    assert.equal(guard({ name: 'exec' }), 'Only structured review completion is permitted')
    assert.ok(options.meta.cwd.includes('tenant-a/optimizer-a/task-1/fence-1'))
    assert.ok(!JSON.stringify(prompt).includes('short-token'))
    assert.equal(completions.length, 1)
    assert.equal(disposed, true)
  } finally { await rm(directory, { recursive: true, force: true }) }
})
