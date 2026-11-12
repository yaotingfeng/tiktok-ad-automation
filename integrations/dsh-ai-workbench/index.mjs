import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { applyRuntime } from './runtime.mjs'

export const inject = ['agents', 'tools', 'llm', 'sessions', 'agentPresets']
export function apply(ctx, config) { applyRuntime(ctx, config, { createUserMessage, SessionId }) }
