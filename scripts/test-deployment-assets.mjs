import { execFileSync } from "node:child_process";
import { test } from "node:test";

const repo = new URL("..", import.meta.url);
for (const suite of ["test-build-release.py", "test-k8s-deployment.py"]) {
  test(suite, () => {
    execFileSync("python3", [new URL(`./${suite}`, import.meta.url).pathname], {
      cwd: repo,
      stdio: "inherit",
    });
  });
}
