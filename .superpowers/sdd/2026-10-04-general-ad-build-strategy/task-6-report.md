# Task 6 report — strategy editor UI

## Implemented

- Replaced the legacy material-group form with the two-layer strategy editor:
  - 广告组数量规则：固定数量 / 按素材数量；对应素材共用、顺序平均分配或每组素材上限。
  - 广告数量规则：固定数量 / 按素材数量；对应本组素材共用、顺序平均分配或每广告素材上限。
  - 每个广告创意数量独立于广告数量。
  - 预算策略：系列预算 / 组预算。
  - 竞价策略：最高价值 / 目标 ROAS；最高价值清空并隐藏目标 ROAS。
- Fixed single-group and single-ad defaults to shared materials and hides arrangement selectors. Mode changes clear fields that no longer belong to the selected mode.
- Updated strategy list, version history, fingerprinting, and the illustrative structure card to use the new business language and show that preview results are authoritative.
- Updated strategy Playwright fixtures and added regression coverage for defaults, conditional fields, clearing behavior, and absence of the old “素材不足处理” choice.

## Verification

- `node_modules/.bin/tsc -p frontend/tsconfig.build.json --noEmit` — passed.
- `git diff --check` — passed.
- Biome check on the six changed frontend files — passed when run from `frontend/` with the nested frontend configuration.
- `bun run test -- tests/strategies.spec.ts` — not runnable in this environment because `bun` is not installed. Direct Playwright invocation also needs the configured Vite web server (`bun run dev`); no live platform/API calls were made.
- `bun run build` — not runnable because `bun` is not installed; TypeScript build-equivalent check passed above.

## Fix round 1

- Long budget values in the strategy list now have `min-w-0` and `wrap-anywhere` constraints on the amount cell.
- ROAS test inputs use `#strategy-target_roas` so they cannot collide with Radix select options.
- Re-ran the focused suite with the local Vite server and Playwright workspace project: **43 passed**.
- `node_modules/.bin/tsc -p frontend/tsconfig.build.json --noEmit`, Biome on all changed strategy files, and `git diff --check` passed.
