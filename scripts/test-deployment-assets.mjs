import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const repo = new URL("..", import.meta.url);
const script = new URL("../deploy/build-and-push.sh", import.meta.url);
const sha = "0123456789abcdef0123456789abcdef01234567";

function run(env = {}) {
  return execFileSync("bash", [script.pathname], {
    cwd: repo,
    env: { ...process.env, ...env },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

test("release script prints the immutable company image in dry-run mode", () => {
  const output = run({ DRY_RUN: "1", RELEASE_SHA: sha });
  assert.match(
    output,
    new RegExp(
      `junbo-image\\.tencentcloudcr\\.com/prod/tiktok-ad-automation:${sha}`,
    ),
  );
  assert.match(output, /docker buildx build/);
  assert.doesNotMatch(output, /latest/);
});

test("release script rejects a non-commit-shaped release id", () => {
  assert.throws(
    () => run({ DRY_RUN: "1", RELEASE_SHA: "latest" }),
    /40-character Git SHA/,
  );
});

test("Kubernetes manifest contains every isolated runtime role", () => {
  const manifest = [
    "tk-ada.yaml",
    "tk-ada-workloads.yaml",
    "migration-job.yaml",
    "runtime-secret.example.env",
  ]
    .map((name) =>
      readFileSync(new URL(`../deploy/k8s/${name}`, import.meta.url), "utf8"),
    )
    .join("\n");
  for (const role of [
    "api",
    "worker-resource",
    "worker-results",
    "worker-builds",
    "worker-control",
    "worker-ads-directory",
    "worker-ads-reporting",
    "worker-ad-management",
    "beat",
  ]) {
    assert.match(manifest, new RegExp(`tiktok-ad-automation-${role}`));
  }
  assert.match(manifest, /tiktok-ad-automation-migration-__RELEASE_ID__/);
  assert.match(manifest, /MCP_CLIENT_REGISTRATION_REF/);
  assert.match(manifest, /S3_ENDPOINT_URL/);
});

test("Kubernetes apply script validates the external runtime files in dry-run mode", () => {
  const fixtureDir = mkdtempSync(join(tmpdir(), "tk-ada-k8s-test-"));
  const runtimeEnvFile = join(fixtureDir, "runtime.env");
  const registrationFile = join(fixtureDir, "mcp-client.json");
  writeFileSync(
    runtimeEnvFile,
    [
      "SECRET_KEY=secret",
      "FIRST_SUPERUSER=admin",
      "FIRST_SUPERUSER_PASSWORD=password",
      "DATABASE_URL=postgresql+psycopg://user:password@postgres.example:5432/app",
      "REDIS_URL=redis://:password@redis.example:6379/0",
      "CONNECTION_ENCRYPTION_KEY=key",
      "S3_ENDPOINT_URL=https://account.r2.cloudflarestorage.com",
      "S3_BUCKET=private",
      "S3_ACCESS_KEY_ID=access",
      "S3_SECRET_ACCESS_KEY=secret",
      'TIKTOK_CALL_POLICIES={"base":{}}',
    ].join("\n"),
  );
  writeFileSync(registrationFile, '{"client_id":"fixture"}\n');
  const output = execFileSync(
    "bash",
    [new URL("../deploy/k8s/apply.sh", import.meta.url).pathname],
    {
      cwd: repo,
      env: {
        ...process.env,
        DRY_RUN: "1",
        K8S_NAMESPACE: "prod",
        RELEASE_ID: sha,
        APP_IMAGE:
          "junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:" + sha,
        RUNTIME_ENV_FILE: runtimeEnvFile,
        MCP_CLIENT_REGISTRATION_FILE: registrationFile,
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  assert.match(output, /namespace=prod/);
  assert.match(output, /migration_job=tiktok-ad-automation-migration-/);
});

test("shared Ingress patch script is append-only and names the project Service", () => {
  const output = execFileSync(
    "bash",
    [new URL("../deploy/k8s/patch-public-ingress.sh", import.meta.url).pathname],
    {
      cwd: repo,
      env: {
        ...process.env,
        DRY_RUN: "1",
        K8S_NAMESPACE: "prod",
        INGRESS_HOST: "tk-ada.137-220-150-31.sslip.io",
      },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  assert.match(output, /append_host=tk-ada\.137-220-150-31\.sslip\.io/);
  assert.match(output, /service=tiktok-ad-automation/);
  assert.match(output, /existing_rules_are_preserved=true/);
});
