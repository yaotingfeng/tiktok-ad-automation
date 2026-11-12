import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import { loadMethods, mountTaskSkills, skillName, hashBody } from './methods.mjs'

// 指向已有 DSH 宿主的 package.json 所在目录；不安装第二套生产运行时。
const root = process.env.DSH_TEST_PACKAGE_ROOT
test('real DSH registry scopes task methods and renders on-demand skill content', { skip: !root }, async () => {
  const require = createRequire(`${root}/package.json`)
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'))
  const { default: SkillRegistry, renderSkillContent } = await import(require.resolve('@deepseek-ai/dsh-skill'))
  const { createScope, scopeOf } = await import(require.resolve('@deepseek-ai/dsh-scope'))
  const host = new Context()
  const registry = await host.plugin(SkillRegistry)
  const consumer = await host.inject(['skills'], async ctx => {
    ctx.skills.register({ name: 'private-host-skill', description: 'unrelated host content', content: 'not task data', source: 'runtime' })
    const first = createScope(ctx, {}), second = createScope(ctx, {})
    try {
      const methods = await loadMethods(), tools = new Map()
      const taskContext = first.ctx.extend({ tools: { register: t => tools.set(t.name, t) } })
      mountTaskSkills(taskContext, methods, { renderSkillContent, scopeOf })
      const newer = await loadMethods()
      newer.main.body += '\nsecond task version'
      newer.main.sha256 = hashBody(newer.main.body)
      mountTaskSkills(second.ctx.extend({ tools: { register() {} } }), newer, { renderSkillContent, scopeOf })
      const main = await tools.get('skill').execute({ name: skillName(methods.main.key) })
      assert.equal(main.content, methods.main.body)
      assert.match(renderSkillContent(main), /skill_content/)
      assert.notEqual((await second.ctx.skills.get(skillName(methods.main.key), { scope: scopeOf(second.ctx) })).content, main.content)
      await assert.rejects(tools.get('skill').execute({ name: 'private-host-skill' }))
      assert.equal(await ctx.skills.get(skillName(methods.main.key)), undefined)
    } finally { await first.dispose(); await second.dispose() }
  })
  await consumer.dispose(); await registry.dispose()
})
