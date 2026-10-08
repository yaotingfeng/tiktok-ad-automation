import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
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
