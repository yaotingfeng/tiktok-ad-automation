# Submission recovery

Recovery is local scheduling over the original submitted, immutable preview. It never reads a newer draft to rebuild the request, never creates provider objects, and never performs TikTok status changes. An original submitter who loses build permission cannot be replaced by an administrator's execution identity.

## HTTP contract

All paths have `/api/tenants/{tenant_id}` as their prefix.

| Operation | Path | Result |
| --- | --- | --- |
| `builds-retry_submission` | POST `/submissions/{submission_id}/retry` | 202 original receipt |
| `builds-reconcile_submission` | POST `/submissions/{submission_id}/reconcile` | 202 original receipt |
| `builds-saved_submission_recovery` | GET `/submission-recovery-requests/{request_id}` | Original receipt, always QUEUED / 0 |
| `builds-get_submission_recovery` | GET `/submission-recoveries/{recovery_id}` | Current scheduling progress |

POST body is exactly `{request_id: UUID}`. Both response views use `RecoveryReceipt`: `recovery_id`, `request_id`, `submission_id`, `kind` (`RETRY` / `RECONCILE`), `state` (`QUEUED` / `RUNNING` / `COMPLETED` / `FAILED`), `scheduled_count`, nullable `reason_code`.

A tenant-scoped request UUID permanently identifies one submission and one kind. A different intent conflicts. Lost-response retries return the original QUEUED / 0 receipt even after completion or failure. Reading/replaying that receipt requires current tenant read permission but never creates work; starting a new request additionally requires current build permission for both the requester and the original submitter. New requests with no safe candidates return `recovery_no_candidates` (409). Both GETs are local and read-only.

COMPLETED means that the recovery scanner finished scheduling eligible steps. Remote verification and creation results remain in the submission's actual step summary. `scheduled_count` counts scheduled steps, not created Ads.

## Safe candidates

Retry requires FAILED or RETRYABLE, SQL NULL request body and remote ID, no REQUEST_ARMED phase or historical send/receipt evidence, no current dispatch or live lease, current matching frozen account authorization/metadata, and satisfied real parent/material dependencies. A failed group's missing material does not block independent groups. Input/Scene changes require a new preview. Known IDs and UNKNOWN never enter the create queue. MATERIAL retry additionally requires no distribution reference, since the material operation owns its remote-send evidence.

Reconcile uses the existing read queue for UNKNOWN, mismatch, and READBACK with an actual parent ID. It retains saved reconciliation page/delivery facts. MATERIAL uses its own scoped distribution and operation: only an existing sending/unknown/verifying/successful operation can be observed. A permission-blocked MATERIAL with an unresolved operation becomes UNKNOWN and queues that same read path. It never enters the advertising GET queue or acquires another upload operation.

Strict material reads carry `read_only=true` through task validation, revision checks, watchdogs, and continuations. The flag rejects preparation and prevents fallback to a fresh upload even if the original operation fails while the message is waiting. A successful original operation with a known VID can revalidate the same receipt; stale revisions cannot refresh it again. The original source advertiser/connection history stays intact. Source-owned unresolved operations remain under the existing source verifier.

After a material distribution finishes, the separate bounded execution repair synchronizes the current verified mapping or blocked result. This lets other recovery candidates proceed immediately.

## Persistence and bounds

`submission_recovery` stores immutable tenant/submission/preview/BC/kind/requester intent plus a durable UUID cursor, counts, a 90-second fenced lease, and the exact current dispatch ID/revision. `submission_recovery_request` is an append-only original receipt. PostgreSQL composite FKs prevent cross-scope references; triggers prevent changing/deleting historical intent. `0011_recovery_evidence_index` bounds historical receipt lookups to each execution step.

The control task `builds.recover_submission` has a 60-second hard / 55-second soft limit, below the 90-second lease. Each call selects at most 100 candidate IDs. Each step uses a separate short transaction: recovery claim, unit lock, parent-before-child readback lock where needed, then step lock; cursor/count and the step's exact outbox dispatch commit together. No SDK or network call occurs while these locks are held. Crashes resume after the last committed cursor. Duplicate workers and different recovery jobs cannot reschedule a step with a current dispatch.

`builds.repair_recoveries` inspects at most 100 expired jobs. It reuses the same dispatch ID and revision; unpublished publisher backoff is preserved. Execution outbox actors remain the original submitter, while recovery scanner outbox actors remain the recovery requester.

Validation uses isolated PostgreSQL databases, actual Redis admission for official SDK wire doubles, and no live provider/TikTok/S3 business operations. This is implementation evidence, not a live-platform certification.
