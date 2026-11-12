import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { renderSkillContent } from '@deepseek-ai/dsh-skill'
import { scopeOf } from '@deepseek-ai/dsh-scope'
import { applyRuntime } from './runtime.mjs'

export const inject = ['agents', 'tools', 'llm', 'sessions', 'agentPresets', 'skills']
export function apply(ctx, config) { applyRuntime(ctx, config, { createUserMessage, SessionId, renderSkillContent, scopeOf }) }
