import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mountKnowledgeTools } from './knowledge.mjs'

test('cancelled knowledge tools never call transport', async () => {
  const tools = new Map(), controller = new AbortController()
  let calls = 0
  const invoke = async () => { calls++; return {} }
  mountKnowledgeTools({ tools: { register: t => tools.set(t.name, t) } }, {}, { searchKnowledge: invoke, readKnowledge: invoke }, controller.signal)
  controller.abort()
  for (const tool of tools.values()) await assert.rejects(tool.execute({ query: '赔付' }), /cancelled/)
  assert.equal(calls, 0)
})
