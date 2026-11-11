# Task 4 report

Implemented optimizer-scoped automatic-rule execution on top of the existing
input/evaluator/executor path.

- `RuleBindingScope` intersects an optimizer's immutable BC/account version with
  the rule account scope and rechecks current grants/authorization revision.
- Scheduled runs fan out per active optimizer binding while ordinary rules keep
  the existing unscoped path.
- Optimizer runs carry rule, optimizer, binding and authorization fences; edits
  or revoked access fail closed immediately before management submission.
- Decision-time `AutomationRuleEvidence` snapshots preserve the rule definition,
  metrics, publication placeholders, checked/hit/skipped objects, management
  task/readback status and action-ledger references beyond the 30-day run cleanup.
- Existing frequency, conflict, preview and management-task code remains the
  single execution engine; manual and automatic authorization checks are reused.

Validation: `uv run ruff check` passed for all changed Python files and
`uv run python -m compileall` passed. Database-backed pytest was not runnable in
this environment because the required test settings/database URL were absent.

Round 1 fixes: unscoped/scoped run uniqueness now uses separate partial unique
indexes; authorization fences retain every account/connection revision;
inactive optimizers are excluded and rejected; evidence includes report sync
rows, dates, timezones, sources and publication versions plus all checked
objects; asynchronous management readback updates the evidence snapshot; and
the executor revalidates the scope immediately before each management submit.
