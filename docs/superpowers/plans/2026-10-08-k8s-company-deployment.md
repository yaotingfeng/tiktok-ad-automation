# TK-ADA Company K8s Deployment Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add the minimum deployment assets needed to build TK-ADA into the company TCR, run it on the company Kubernetes cluster, and reuse the current staging PostgreSQL, Redis, R2 object storage, and callback configuration without committing credentials.

**Architecture:** Keep the existing FastAPI/Celery application image and PostgreSQL/Alembic model unchanged. Add plain Kubernetes manifests for the API, isolated Celery workers, one Beat, and a per-release migration Job; inject the existing environment through a Kubernetes Secret and mount the existing MCP registration JSON as a separate Secret. Use the shared `public` Ingress through a guarded append-only patch script.

**Tech Stack:** Docker Buildx, Tencent Cloud TCR, Kubernetes `kubectl`, Bash, existing FastAPI/Celery/PostgreSQL/Redis/R2 stack.

**Spec:** `deploy-help 2/SKILL.md` and the user's instruction to preserve PostgreSQL and reuse current test object storage/callback settings.

## Global Constraints

- PostgreSQL remains the application database; do not introduce a MySQL migration.
- Do not commit `.env`, object-storage credentials, OAuth secrets, MCP registration material, kubeconfig, tokens, or runtime sessions.
- Use the project-prefixed resource name `tiktok-ad-automation` and the TCR image path `junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:<git-sha>`.
- Keep the application listening on container port 8000 and expose Kubernetes Service port 80 to targetPort 8000.
- Preserve separate resource, result, build, control, ads-directory, ads-reporting, ad-management, and single Beat processes.
- Reuse the current staging callback and object-storage values only through external runtime configuration; do not copy their secrets into Git.
- Shared Ingress changes must be append-only and must refuse an existing host before patching.

## Review Focus

- A missing runtime secret must fail before deployment rather than start a partially configured worker.
- A repeated migration release must use a unique Job name and must not run multiple Beat instances.
- A shared Ingress patch must refuse duplicate hosts and preserve every existing rule.
- A test object-storage secret must be mounted into all API/Worker/Beat processes, while the MCP registration file must be mounted at the path injected into `MCP_CLIENT_REGISTRATION_REF`.
- A release image must be tied to a full Git SHA and must not silently fall back to a mutable `latest` tag.

### Task 1: Add the reproducible TCR release command

**Files:**
- Create: `deploy/build-and-push.sh`
- Test: `bash -n deploy/build-and-push.sh`

**Interfaces:**
- Consumes: repository Git SHA and a Docker Buildx installation.
- Produces: `junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:<full-sha>` pushed with the existing multi-stage `backend/Dockerfile`.

- [ ] Validate a full SHA, require a clean tracked worktree, and require Docker before building.
- [ ] Build and push an immutable SHA tag; never use `latest`.
- [ ] Run `bash -n` and a dry-run argument check without network writes.

### Task 2: Add Kubernetes application resources

**Files:**
- Create: `deploy/k8s/tk-ada.yaml`
- Create: `deploy/k8s/runtime-secret.example.env`
- Create: `deploy/k8s/mcp-client-registration.example.json`
- Create: `deploy/k8s/apply.sh`

**Interfaces:**
- Consumes: an external runtime env file (the current staging `.env.staging` or equivalent) and an MCP registration JSON file.
- Produces: API/Service, isolated Celery Deployments, one Beat, PVC, and a per-release migration Job using the same image and external PG/Redis/R2 configuration.

- [ ] Add all eight runtime roles plus the migration Job with project-prefixed names.
- [ ] Inject the runtime Secret into every role and mount the MCP registration Secret.
- [ ] Keep current staging callback defaults in the example files while requiring real values through the external env file.
- [ ] Add API probes and Service port 80 to targetPort 8000.
- [ ] Generate a unique migration Job from the release SHA and wait for completion in `apply.sh`.
- [ ] Run shell syntax and placeholder/manifest structure checks.

### Task 3: Add a guarded shared-Ingress patch

**Files:**
- Create: `deploy/k8s/patch-public-ingress.sh`

**Interfaces:**
- Consumes: `K8S_NAMESPACE`, `INGRESS_HOST`, `tiktok-ad-automation` Service, and the existing shared Ingress `public`.
- Produces: one appended HTTPS host rule that routes to the project Service, refusing duplicate hosts.

- [ ] Read and validate the existing Ingress before patching.
- [ ] Refuse a host already present and refuse a missing project Service.
- [ ] Apply only an append operation and never delete or rewrite existing rules.
- [ ] Run `bash -n` and a no-network argument validation path.

### Task 4: Document the minimal deployment and current-staging reuse

**Files:**
- Create: `deploy/k8s/README.md`
- Modify: `README.md`
- Modify: `docs/runbooks/deployment.md`
- Modify: `config/README.md`

**Interfaces:**
- Documents: GitHub origin plus company `company` remote, TCR image flow, Kubernetes commands, PostgreSQL exception, current staging object-storage/callback reuse, and secret handling.

- [ ] State that current staging object storage/callback values are supplied externally and are not committed.
- [ ] Record the exact runtime secret keys and MCP file mount path.
- [ ] Document the required backup, migration, rollout, worker, Beat, and read-back checks.
- [ ] Run Markdown/link and diff checks.

### Task 5: Verify, commit, and push to company Git

**Files:**
- Modify only files from Tasks 1–4.

- [ ] Run shell syntax checks, repository deployment regression tests, and a final diff/status review.
- [ ] Commit only the deployment changes; leave the user's pre-existing `AGENTS.md` modification untouched.
- [ ] Add the Gitea remote as `company` without replacing GitHub `origin`.
- [ ] Push the new commit to `company main`; report any authentication failure with the exact external prerequisite.
