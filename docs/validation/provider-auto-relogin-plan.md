# Bounded implementation plan: automatic provider session renewal

1. Add real PostgreSQL failures for expiry continuation, connection-level contention, tenant isolation and stale login results.
2. Persist one renewal record per tenant connection; claim/commit before each single HTTP call and fence receipt writes by connection version, generation and lease.
3. Continue through original item/outbox/watchdog; preserve application generation for unchanged facts and never replay an unknown business write.
4. Classify transient network errors, wrong credentials and forbidden applications separately; bound each retry burst, persist a ten-minute cooldown before automatic recovery, and store due times instead of sleeping. Candidate expiry restarts bounded authentication; worker soft deadlines preserve recoverable phase.
5. Show ordinary expiry as automatic recovery; retain administrator credential editing for true failures.
6. Run provider, migration, type and browser regressions offline, then hand off migration parenting and integration.

Authorization: user explicitly requested automatic relogin for the implemented Wangyan and Jiashu providers. No additional provider brand is inferred from an informal contact name. Root owns unrelated identity migration, generated client and shared documentation.
