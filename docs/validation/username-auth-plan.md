# Username authentication implementation

1. Reproduce the username API and removed recovery contracts with tests.
2. Add normalized username validation and database constraints; migrate existing identities deterministically in 0015 without replacing users or hashes.
3. Remove system email/SMTP/recovery functionality and update protected admin and self-service account APIs, tenant candidate queries and actor display.
4. Update login/admin/member UI and fixtures; regenerate the client from actual OpenAPI. Keep provider email contracts unchanged.
5. Verify migration with real PostgreSQL and colliding/non-ASCII identities, auth/tenant API regression, full TypeScript/build and workspace browser tests.
6. Document private backup/mapping and root rollout steps; deliver an isolated commit. Root owns main DB migration and deployment.
