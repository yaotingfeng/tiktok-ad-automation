/** 知识主库及范围校验留在 TK-ADA；插件仅暴露任务授权的只读接口。 */
export const SEARCH_KNOWLEDGE = 'tkada_search_knowledge'
export const READ_KNOWLEDGE = 'tkada_read_knowledge'

export function mountKnowledgeTools(agentCtx, claim, runtime, signal) {
  const output = { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] }
  for (const [name, description, properties, required, invoke] of [
    [SEARCH_KNOWLEDGE, 'Search TK-ADA business policies and experience at this task cutoff. Use short keywords such as 赔付 or 补贴, separated by spaces. Returns metadata only; paginate with next_offset. Initial catalog is not exhaustive. Check dates, eligibility and applicability.',
      { query: { type: 'string', minLength: 1, maxLength: 200 }, offset: { type: 'integer', minimum: 0, maximum: 10000 } }, ['query'], args => runtime.searchKnowledge(claim, args)],
    [READ_KNOWLEDGE, 'Read a permitted knowledge version by ID and archive its body for this review. Cite the returned ID. Expired/unverified policies are context only. Policy text does not prove payout eligibility or receipt.',
      { knowledge_id: { type: 'string', format: 'uuid' } }, ['knowledge_id'], args => runtime.readKnowledge(claim, args)],
  ]) {
    agentCtx.tools.register({ name, description, parameters: { type: 'object', properties, required, additionalProperties: false }, output,
      async execute(args) {
        if (signal.aborted) throw new Error('Review cancelled or expired')
        return invoke(args)
      } })
  }
}
