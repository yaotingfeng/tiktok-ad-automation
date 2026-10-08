# TK-ADA Kubernetes deployment

This directory adds the minimum Kubernetes assets for TK-ADA. The application database remains PostgreSQL; this deployment does not run PostgreSQL or Redis inside the application namespace. Supply cluster-reachable PostgreSQL and Redis URLs through the external runtime env file.

The current staging object-storage and callback behavior is kept as the default in `tk-ada.yaml` and `runtime-secret.example.env`:

- `OBJECT_STORAGE_PROVIDER=r2` and `S3_REGION=auto`
- the current staging R2 endpoint, bucket, access key and secret are supplied privately at deployment time
- MCP callback: `https://tk-ada.137-220-150-31.sslip.io/api/integrations/tiktok/mcp/callback`
- current staging material ingest and cleanup defaults remain enabled

Credentials, the real MCP registration JSON, kubeconfig and `.env.staging` must remain outside Git. When moving the existing staging database into Kubernetes, keep the same `CONNECTION_ENCRYPTION_KEY`; otherwise encrypted provider and TikTok connection records cannot be read.

The repository keeps the existing GitHub `origin` and uses the company repository as a second remote:

```bash
git remote add company https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git
git push company main
```

## Prepare the runtime files

Copy `runtime-secret.example.env` outside the repository and fill in all values. Start from the current staging private environment if appropriate, but replace its loopback `DATABASE_URL` and `REDIS_URL` with endpoints reachable from the Kubernetes cluster. `apply.sh` rejects loopback database and Redis URLs.

Copy the current staging MCP registration JSON to a private path. The Kubernetes mount path is `/run/tk-ada/mcp-client.json`; the deployment injects that path into `MCP_CLIENT_REGISTRATION_REF` for API, every Worker and Beat.

The R2 values are injected as follows:

- `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`: runtime Secret
- `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION`, `OBJECT_STORAGE_PROVIDER`: runtime configuration
- Configure the private bucket CORS for the final `FRONTEND_HOST`, allowing browser PUT/GET/HEAD and exposing `ETag`; do not make the bucket public.

## Build and deploy

Log in to the company TCR first. The release script uses the full Git SHA and never publishes `latest`:

```bash
docker login junbo-image.tencentcloudcr.com
bash deploy/build-and-push.sh
```

Then prepare the Kubernetes environment and run the migration before the workloads. The script creates or updates only the two project-prefixed Secrets, applies the project static resources, creates a per-SHA migration Job, waits for it to complete, and then rolls out the API, isolated Workers and the single Beat.

```bash
export K8S_NAMESPACE=prod
export RELEASE_ID="$(git rev-parse HEAD)"
export APP_IMAGE="junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:${RELEASE_ID}"
export RUNTIME_ENV_FILE="/private/path/tk-ada-staging-runtime.env"
export MCP_CLIENT_REGISTRATION_FILE="/private/path/mcp-client.json"

bash deploy/k8s/apply.sh
```

The namespace must already exist and the selected kubeconfig must be the project-scoped credential. No `imagePullSecrets` is included because the company cluster supplies image-pull access internally.

## Add the external host

The deployment does not create a second Ingress. Append the project host to the shared `public` Ingress only after checking its current rules:

```bash
export K8S_NAMESPACE=prod
export INGRESS_HOST="tk-ada.137-220-150-31.sslip.io"
bash deploy/k8s/patch-public-ingress.sh
```

The patch refuses an existing host, verifies that `tiktok-ad-automation` is the project-labeled Service, appends one rule, and reads the new host back. It does not remove or rewrite any existing host rule.

## Verification

```bash
kubectl -n "$K8S_NAMESPACE" get deploy,svc,pods -l app=tiktok-ad-automation
kubectl -n "$K8S_NAMESPACE" get job -l app=tiktok-ad-automation,role=migration
kubectl -n "$K8S_NAMESPACE" logs deployment/tiktok-ad-automation-api --tail=100
kubectl -n "$K8S_NAMESPACE" exec deployment/tiktok-ad-automation-worker-resource -- \
  celery -A app.jobs.celery_app:celery_app inspect ping --timeout 10
```

The API probe only proves that the process responds. Before enabling real TikTok or MCP work, verify the actual PostgreSQL/Redis configuration, R2 signed upload/readback, the callback URL, `TIKTOK_CALL_POLICIES`, tenant authorization and BC/account reads separately.
