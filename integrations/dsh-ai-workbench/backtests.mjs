/** 仅重放 TK-ADA 已冻结规则事实，不读取最新平台数据、不执行动作。 */
export const BACKTEST_RULE = 'tkada_backtest_rule'
export function mountBacktestTool(agentCtx, claim, runtime, signal) {
  agentCtx.tools.register({ name: BACKTEST_RULE, description: 'Test a candidate rule against one frozen rule_evidence ID. Only captured accounts/ranges/metrics are supported. Archives deterministic hit differences; never proves causal savings or false pauses. Cite returned ID and report limitations.',
    parameters: { type: 'object', properties: { evidence_id: { type: 'string', format: 'uuid' }, candidate_definition: { type: 'object', additionalProperties: true } }, required: ['evidence_id', 'candidate_definition'], additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args) {
      if (signal.aborted) throw new Error('Review cancelled or expired')
      return runtime.backtestRule(claim, args)
    }
  })
}
