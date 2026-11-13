import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mountBacktestTool, BACKTEST_RULE } from './backtests.mjs'

test('backtest tool uses task runtime and blocks cancelled calls', async () => {
  const tools = new Map(), controller = new AbortController(), claim = { task_token: 'task-secret' }
  const args = { evidence_id: 'frozen-id', candidate_definition: { conditions: [] } }
  let calls = 0
  mountBacktestTool({ tools: { register: t => tools.set(t.name, t) } }, claim, { backtestRule: async (identity, input) => {
    assert.equal(identity, claim); assert.deepEqual(input, args); calls++; return { id: 'archived-backtest', limitations: [] }
  } }, controller.signal)
  const tool = tools.get(BACKTEST_RULE)
  assert.equal((await tool.execute(args)).id, 'archived-backtest')
  controller.abort()
  await assert.rejects(tool.execute(args), /cancelled/)
  assert.equal(calls, 1)
})
