import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'

export async function prepareRuntime({ home, workspace, providerFile, tokenFile, apiUrl, port = 9080 }) {
  const provider = JSON.parse(await readFile(providerFile, 'utf8'))
  if (!provider.provider || !provider.model || !provider.config?.apiKeyEnv || !process.env[provider.config.apiKeyEnv]) throw new Error('Configure the selected DSH model and credential reference')
  if (!(await readFile(tokenFile, 'utf8')).trim() || (await readFile(tokenFile, 'utf8')).trim().length < 32) throw new Error('Configure a private TK-ADA service credential')
  const url = new URL(apiUrl)
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname))) throw new Error('TK-ADA requires HTTPS')
  if (url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('TK-ADA requires a plain origin')
  const profile = join(home, 'profiles', 'tkada')
  await mkdir(profile, { recursive: true, mode: 0o700 })
  await mkdir(workspace, { recursive: true, mode: 0o700 })
  const patch = [
    { id: 'plugin-manager', disabled: true }, { id: 'hmr', disabled: true }, { id: 'config-editor', disabled: true },
    { id: 'settings', disabled: true }, { id: 'session-title-llm', disabled: true }, { id: 'session-telemetry-otel', disabled: true },
    { id: 'llm-pi-ai', config: { providers: { [provider.provider]: provider.config } } },
    { id: 'agent-default-model', config: { provider: provider.provider, model: provider.model } },
    // base 没有 Web profile 的 preset 注册表；显式注册空 standard，任务自身挂载受限工具。
    { insert: [
      { id: 'agent-preset-registry', name: '@deepseek-ai/dsh-agent-preset-registry', config: { default: 'standard' } },
      { id: 'preset-standard', name: '@deepseek-ai/dsh-agent-preset', config: { id: 'standard', plugins: [] } },
      { id: 'tkada-reviews', name: join(dirname(fileURLToPath(import.meta.url)), 'service.mjs'), config: { url: apiUrl, tokenFile, stateRoot: join(dirname(home), 'reviews'), modelSelection: { provider: provider.provider, model: provider.model }, healthPort: Number(port) } },
    ] },
  ]
  await writeFile(join(profile, 'package.json'), JSON.stringify({ name: 'dsh-profile-tkada', private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }), { mode: 0o600 })
  // 状态卷只保存部署配置引用，不复制用户个人会话或明文模型凭据。
  await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify(patch), { mode: 0o600 })
  return profile
}

if (import.meta.main) {
  const home = process.env.DSH_HOME || '/var/lib/dsh/home'
  const workspace = process.env.DSH_WORKSPACE || '/var/lib/dsh/workspace'
  await prepareRuntime({ home, workspace, providerFile: process.env.DSH_PROVIDER_FILE || '/run/secrets/provider.json', tokenFile: process.env.AI_DSH_TOKEN_FILE || '/run/secrets/tkada-ai-review-token', apiUrl: process.env.TKADA_API_URL, port: process.env.PORT || 9080 })
  const cli = process.env.DSH_CLI || '/opt/tkada-dsh/node_modules/@deepseek-ai/dsh/lib/bin.js'
  const child = spawn(process.execPath, [cli, '--profile', 'tkada'], { cwd: workspace, stdio: 'inherit', env: { ...process.env, DSH_HOME: home } })
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal))
  child.on('error', () => { console.error('DSH failed to start'); process.exitCode = 1 })
  child.on('exit', code => { process.exitCode = code ?? 1 })
}
