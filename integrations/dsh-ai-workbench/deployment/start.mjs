import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'

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
  // Kubernetes fsGroup 重新挂载可能将官方凭据文件改为0660；
  // 仅收紧宿主自身凭据文件，保留原内容，满足DSH的owner-only约束。
  try { await chmod(join(home, '.credentials.yaml'), 0o600) } catch (error) { if (error.code !== 'ENOENT') throw error }
  const patch = [
    // Web 标准预设依赖 pluginManager；关闭会同时阻断会话创建和插件管理页。
    { id: 'plugin-manager', disabled: false }, { id: 'hmr', disabled: true },
    { id: 'session-title-llm', disabled: true }, { id: 'session-telemetry-otel', disabled: true },
    { id: 'llm-pi-ai', config: { providers: { [provider.provider]: provider.config } } },
    { id: 'agent-default-model', config: { provider: provider.provider, model: provider.model } },
    // 复用官方 Web profile；禁止打印带凭据 URL，原生监听只在 Pod 回环。
    // 容器没有系统 Documents/xdg-user-dir；默认工作区必须落在持久卷内。
    { id: 'workspace-controller', config: { documentsDirectory: workspace } },
    { id: 'webserver', config: { host: '127.0.0.1', port: 9082 } },
    { id: 'web-runtime', config: { openBrowser: false, printUrl: false, surfaceContext: false, publicUrl: `${url.origin}/dsh/` } },
    { insert: [
      { id: 'tkada-admin-proxy', name: join(dirname(fileURLToPath(import.meta.url)), 'admin-proxy.mjs'), config: { apiUrl } },
      { id: 'tkada-reviews', name: join(dirname(fileURLToPath(import.meta.url)), 'service.mjs'), config: { url: apiUrl, tokenFile, stateRoot: join(dirname(home), 'reviews'), healthPort: Number(port) } },
    ] },
  ]
  await writeFile(join(profile, 'package.json'), JSON.stringify({ name: 'dsh-profile-tkada', private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'] } } }), { mode: 0o600 })
  // 官方 Models 页写入 profile patch；重启必须保留管理员设置，只刷新部署拥有的安全/接入行。
  // 生产镜像从 deployment/package.json 解析依赖；本地原生验收可指向
  // 已安装官方 DSH 宿主，避免隐式读取开发者其他项目的依赖树。
  const packageRoot = resolve(process.env.DSH_PACKAGE_ROOT || dirname(fileURLToPath(import.meta.url)))
  const { parseDocument } = createRequire(join(packageRoot, 'package.json'))('yaml')
  const patchFile = join(profile, 'cordis.patch.yml')
  let previous
  try { previous = await readFile(patchFile, 'utf8') } catch (error) { if (error.code !== 'ENOENT') throw error }
  if (previous) {
    const document = parseDocument(previous)
    if (document.errors.length || !Array.isArray(document.toJSON())) throw new Error('Invalid DSH profile patch')
    const managed = new Set(patch.flatMap(row => row.insert || [row]).map(row => row.id).filter(id => !['llm-pi-ai', 'agent-default-model'].includes(id)))
    // 旧 base profile 曾把这两个 preset 行写入用户 patch；Web profile
    // 会再次注册同名 id，必须迁移删除，否则 Loader 会因重复 id 拒绝启动。
    const obsolete = new Set(['settings', 'config-editor', 'agent-preset-registry', 'preset-standard'])
    for (let index = document.contents.items.length - 1; index >= 0; index--) {
      const row = document.contents.items[index]
      if (managed.has(row.get('id')) || obsolete.has(row.get('id'))) document.contents.items.splice(index, 1)
      else if (row.has('insert')) {
        const rows = row.get('insert').items
        for (let child = rows.length - 1; child >= 0; child--) {
          if (managed.has(rows[child].get('id')) || obsolete.has(rows[child].get('id'))) rows.splice(child, 1)
        }
      }
    }
    for (const row of patch) {
      if (!['llm-pi-ai', 'agent-default-model'].includes(row.id)) document.contents.add(row)
    }
    await writeFile(patchFile, String(document), { mode: 0o600 })
  } else await writeFile(patchFile, JSON.stringify(patch), { mode: 0o600 })
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
