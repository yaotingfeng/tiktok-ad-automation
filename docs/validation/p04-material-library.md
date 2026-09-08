# P04 Task 1: material library and literal title matching

Implemented the seven planned material, upload, mapping and operation tables. Migration `0004_materials` follows `0003_provider_links`. It creates `pg_trgm`, a GIN filename search index and a tenant/BC/name/ID ordering index. Filename uses PostgreSQL `C` collation directly, avoiding expression-index reflection drift while preserving the required bytewise ordering. Downgrade removes only this module's tables/indexes and retains the shared extension.

No material row contains a drama owner. ORM inserts and filename updates compute Unicode casefolded filenames; matching trims only the supplied title, uses literal SQL containment with wildcard escaping, and never loads the entire catalog into Python. The signed cursor binds tenant, BC, normalized title and the last filename/UUID. Pages contain 100 files and return `next_cursor`.

`app.modules.materials.service.match_materials` exports the planned cross-module interface. `MaterialCandidate.source_assets` contains **at most one representative verified location**, selected in SQL per file, so distributing one file across thousands of advertisers cannot expand every candidate page by thousands of mappings. Readiness/distribution must query all eligible mappings in the database; the later asset detail endpoint paginates all recorded locations. This bounded candidate field must not be treated as a complete inventory.

The schema preserves actual upload advertiser/connection/operation identities with composite tenant/BC/material references. Account mappings reference the exact recorded BC/account/connection grant and require a verified video identity before becoming available. Source uploads and target sharing compete for one database-enforced unfinished operation per tenant/material/target; changing the path does not bypass this constraint. Original file size fields use bigint, including a 4 GB fixture.

`UploadBatch` additionally stores its request digest and creation time. `ObjectUpload` stores BC, part size, local task ID and a safe error code. `MaterialAssetOperation` includes BC and a claim deadline; pending/sending/result_unknown/verifying/confirmed_absent remain mutually exclusive per target. Sending requires an attempt token. `MaterialDistribution` includes the original actor and optional source/operation links. These fields support the subsequent upload/distribution workers without inventing a permanent material account.

Validation on 2026-09-09:

- Initial matching test failed because the material module did not yet exist.
- 20 material tests pass as part of **401 passing backend tests** after P03 schema integration. Coverage includes 205-file 100/100/5 pagination, same filenames with distinct UUIDs, `%`, `_`, backslash and Unicode casefold, two-drama matches, incomplete originals, representative mappings, signed cursor scope, cross-tenant foreign keys and unverified video rejection.
- Two actual PostgreSQL sessions race an original upload against a share for the same target: exactly one commits. The committed tenant fixtures are removed by explicit tenant ID afterward.
- A fresh disposable PostgreSQL database upgrades from base to `0004_materials`, passes `alembic check`, downgrades to `0003_provider_links`, upgrades again and passes a second drift check. The owned database is dropped afterward.
- Scoped Ruff, mypy and ty pass. Root integration uses isolated PostgreSQL and Redis; no S3 or TikTok calls occur in this task.

This delivers Task 1 only. Multipart receiving, actual SDK upload, distribution/readiness and the material UI are subsequent tasks. No claim is made that live videos have been uploaded or verified.
