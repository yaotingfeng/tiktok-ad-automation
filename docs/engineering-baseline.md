# Engineering baseline

- Origin: `git@github.com:yaotingfeng/tiktok-ad-automation.git`; integration branch `feat/platform-implementation`.
- Official FastAPI full-stack template: `cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7`, imported with `git archive`, license preserved. `template` is the read-only reference remote.
- Official TikTok SDK: `f809c396520df2d7b201a9ccc5378d822b728ed3`, `python_sdk` subdirectory. Installed distribution `python-sdk 1.0.0`, import `business_api_client`.
- Workspaces use **root `uv.lock` and root `bun.lock`**. Earlier plan paths backend/uv.lock and frontend/bun.lock are superseded by the actual pinned template layout.
- Verified locally: Python 3.14.6, uv 0.7.6, Bun 1.4.2, Vite 8.2.0. Project-local Bun is available at `.tools/node_modules/.bin/bun` when Bun is absent from PATH.
- Python locked packages: Celery 5.6.3, Redis client 6.4.0, cryptography 50.0.1, boto3/botocore 1.43.89. Full transitive versions are in `uv.lock`.
- Local integration services: PostgreSQL 17.5 and Redis 8. Production Compose/CI target PostgreSQL 18 and Redis 8. Local dedicated test databases are separate from `tiktok_dev`; test Redis uses a separate nonzero DB and UUID key ownership.
- Frozen uv install, frozen Bun install, generated API client and frontend production build passed. SDK has 11 offline wrapper/transport contract tests and independent PASS review; no live TikTok compatibility claim.
- SDK video upload requires multipart keyword arguments, unlike JSON-body Smart+ create/share wrappers. Minis-specific scene fields remain P06/P07 validation scope.
- Docker Compose 5.5.1 parsed both local and explicit staging configurations. Docker daemon is unavailable locally; image build is assigned to GitHub CI and its result must be recorded separately.
- Original reference documentation and CLI credentials remain in the source repository; no operational output, token or real account default was imported.
