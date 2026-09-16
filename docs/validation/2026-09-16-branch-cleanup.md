# 2026-09-16 分支整理

## 主分支与处理结果

- 用户要求保留一个主分支作为后续服务器发布来源，核对并清理其余分支。
- 整理前 GitHub 默认分支、本地当前分支均为 `feat/platform-implementation`，本地没有 `main`，共有 67 个本地分支、10 个 origin 远端分支。
- 已将当前集成分支原位更名为 `main`，保留完整历史，清理其他 66 个本地分支名。没有合并旧树或更改应用代码。
- 祖先关系及 `git cherry` 核对表明，多数旧任务提交已经挑选进入集成分支。对补丁不等价的提交进一步核对对应集成提交、最终文件及后续重构记录：历史锁顺序/授权恢复测试已完整保留；生成客户端已随后更新；旧上传确认逻辑已由 `182d2b4` 整合并在 `7582056` 改为新上传管线；旧 SDK/搭建额度逻辑已由 gateway 统一准入替代。无需再次合并这些旧业务分支。
- 4 个 Dependabot 分支是旧基线的依赖更新，未验证与当前应用的兼容性，不合入本次发布主线，完整提交已归档；后续需要升级时应基于最新 `main` 重新评估。
- 70 条工作树记录中，一条 `/private/tmp/p03-task34-review` 目录已不存在，已清理失效登记。其他工作树保留原路径、原提交和全部文件，旧分支改为 detached HEAD；保留 3 个工作目录的未提交代码修改及环境依赖。工作目录清理不在本轮分支删除范围内，禁止据此删除未提交文件。
- 远端尚未修改：等待用户明确授权推送 `main`、设为 GitHub 默认分支并删除 10 个旧远端分支。本地没有绑定旧远端上游。
- 本轮没有发布或修改服务器。

## 可恢复备份

本地私有归档：`/Users/yaotingfeng/Documents/ytf/ytf-os-ad-skill/archive/tiktok-ad-automation/branch-cleanup-20260916-103354`。

`all-branches.bundle` 包含所有本地与远端引用和工作树提交，`git bundle verify` 已通过；`SHA256SUMS` 保存校验和。`refs-before.txt`、`worktrees.json`、`worktrees-before.txt` 记录原分支与工作目录；3 个二进制格式差异补丁保存未提交修改；原文件亦保持原位。需要恢复旧分支时，从 bundle 按 `refs-before.txt` 中的原引用提取，恢复前检查现有分支和工作目录。

## 验证

- 已刷新 origin 并读取真实远端默认分支。
- 清理前后逐个验证保留工作树的 HEAD 与 `git status --porcelain` 完全一致。
- 本地仅 `main`；业务文件与 `4f571406cab2690d04a2be16d18df042ef70ffe7` 完全一致，本轮仅补充分支及发布文档。
- 基线的前后端回归、构建及测试环境发布证据沿用 [分页验收](2026-09-15-pagination-total-counts.md)，不把分支整理视为重新执行应用测试或生产发布验证。

## 整理前引用清单

| 引用 | 提交 |
| --- | --- |
| `refs/heads/docs/r2-upload-plan` | `19e35b861f8c9ce204181a5192d90de608713bbb` |
| `refs/heads/feat/platform-implementation` | `4f571406cab2690d04a2be16d18df042ef70ffe7` |
| `refs/heads/feat/provider-auto-relogin` | `c685521b3c40f099cbc576000907f2ae81ce79bc` |
| `refs/heads/feat/r2-batch-video-upload` | `46f4f8c03a2e77763d5a4f31c3a640b69a97eb28` |
| `refs/heads/feat/shadcn-official-alignment` | `4c732a20937a00ef18da04dde17d861baf9456d4` |
| `refs/heads/feat/strategy-visual-refinement` | `601c181d1cca7b62fcbd8248cd5db8be87f96d07` |
| `refs/heads/feat/username-auth` | `310019824d11c477fa579c6b534bbd89dd6eae4f` |
| `refs/heads/feat/workspace-visual-rollout` | `96c7b14d6010365e8f511a9ffcfd48aa2e579617` |
| `refs/heads/fix/platform-navigation-usability` | `ea8d9f152d742a47d287de33fc03c03c8106eb59` |
| `refs/heads/fix/strategy-currency-usability` | `94b3b92f2040db97d7c4075952fc5d5cb4bf15d8` |
| `refs/heads/fix/workspace-bc-empty` | `863c6d7329954954dee39c3d1f99be92d4dbc6db` |
| `refs/heads/review/material-read-apis` | `ce45d8e12b5b4ec0b4493f3a106e2e66928a8d9a` |
| `refs/heads/review/p04-materials` | `e0db9e83bc17a08bbec7c489ae014d6f90203bbb` |
| `refs/heads/review/p06-capability-bootstrap` | `f2f01d2bfb723f58204230c58b8cc9aa04989fd9` |
| `refs/heads/review/p06-execution` | `1e872a864c3e2ce4457be45904bd44191837d819` |
| `refs/heads/review/p07-queue-fairness` | `1e7ebf28a244da18a6277a5ffffea79a71da555d` |
| `refs/heads/review/p07-summary-capacity` | `f7d730e8ea2195235019bd057151ef2dd53e4fba` |
| `refs/heads/review/p07-wangyan` | `c5e88641a9e636bebcc19d6486c8f92029553df9` |
| `refs/heads/review/prefork-cleanup` | `71ac5a80203ee6d10b6c63faefd34380ea9c6897` |
| `refs/heads/review/preview-freeze` | `4fe7994f2a422cfb0f4a80d62baa230e59bc9bbe` |
| `refs/heads/task/p01-backend-core` | `28aa793fd8a5179c0bce820437af63ce2a0ab9f2` |
| `refs/heads/task/p01-jobs` | `1e733dc34d6d0873e325b0ba2cd77bde94484a5e` |
| `refs/heads/task/p01-sdk` | `dc4d3b08f4ea13939ed4339a4c88b4f16da099df` |
| `refs/heads/task/p01-workspace-ui` | `ead442e4c717dfa342dee80a54e67102d6933afa` |
| `refs/heads/task/p02-accounts` | `3306aa865cd1d30d1d5cf8e62299736513980b9a` |
| `refs/heads/task/p02-discovery` | `b9c4f0a26edb0325a997ea9dcbdeb4734bbc18fd` |
| `refs/heads/task/p03-outbox-recovery-fix` | `9ad0f2a2b980a2f2363cf3df7204de7f104cad44` |
| `refs/heads/task/p03-provider-protocols` | `4a38b0bf9c131bac10630f314912a22b0567f2f8` |
| `refs/heads/task/p04-material-read-apis` | `4f1eb599fd19f1f358843ba3009c8364df3b5f25` |
| `refs/heads/task/p04-materials-ui` | `3a89e1632f433f3966dccf57fb2e7e3800581a84` |
| `refs/heads/task/p04-object-uploads` | `3aaadc02684b73b543f65b38a70d5eb3b47ff01a` |
| `refs/heads/task/p04-sdk-assets` | `76f5d0c4209bd1c36842cdde41a452656c19223a` |
| `refs/heads/task/p05-builds-ui` | `a1e42994dda69af20f71e99cef78ce95f8e4d596` |
| `refs/heads/task/p05-strategies-ui` | `2534123337a3b92d5ce226eb17dcadab08f06c08` |
| `refs/heads/task/p06-account-capabilities` | `c0ef71541c1399622f1fa264979a753c20b13681` |
| `refs/heads/task/p06-reconciliation` | `08e40605703cf20c1df3ec92153b7724adbf4a37` |
| `refs/heads/task/p06-recovery` | `cf61a2dde6cbad412ac1ac868e99cba33b6f6158` |
| `refs/heads/task/p06-scene-context` | `5d6c82f0f3ddc451ec6a4c804ffe312d976e9d62` |
| `refs/heads/task/p06-scene-preparation` | `79f3806b18a154332baa60a2e17c8cf26a146e30` |
| `refs/heads/task/p06-submissions` | `f0d152ffea37332a0f264358dbb1aa9a616de600` |
| `refs/heads/task/p06-submissions-ui` | `382b9ee2f4267230ab543f1afaa2c442ca14a31e` |
| `refs/heads/task/p07-acceptance` | `0d2c8f39825429fd933b664c133a0367fc0f8764` |
| `refs/heads/task/p07-acceptance-ui` | `52cd03f97c8cac219d696f108652cfeb1de87250` |
| `refs/heads/task/p07-capacity-delivery` | `fc9b0829b97f6f2d743d145165f6a3eb9e14b2c7` |
| `refs/heads/task/p07-capacity-validation` | `aa2a7c44eb749fc20e229e358ee98ce201d1e6f9` |
| `refs/heads/task/p07-directory-revision` | `b8230536bed9d4f93b3cd87b69426cba0f90cdfe` |
| `refs/heads/task/p07-recovery-summary` | `e65f4bd19643e41e7efb3a48310211058911cfca` |
| `refs/heads/task/p07-summary-source` | `2e0375d26888c9187d012a2932f496e76a571135` |
| `refs/heads/task/r2-browser-acceptance` | `f973cf5612b484e67fc01371375a65135e9a29f8` |
| `refs/heads/task/r2-browser-transfer` | `8c695390ac848b074bc5ee0e62079eacc9367883` |
| `refs/heads/task/r2-compose-env` | `f20d373c68cd071bcccf26792560dadf8d4880a8` |
| `refs/heads/task/r2-deployment-capacity` | `c25ac71c6497fda2d6a89c894d4fb3abb01bcf4d` |
| `refs/heads/task/r2-ingest-models` | `a2bec7bcebddc92ebd5b5875a800e6e34270bd5c` |
| `refs/heads/task/r2-offline-runtime` | `46f57a01754fe93dbce45e6246b438b52fceebbc` |
| `refs/heads/task/r2-pipeline-acceptance` | `593a7d71ff3e3fab8cf9ca19f0b1174ee5dd0878` |
| `refs/heads/task/r2-remote-distribution` | `7939f4a2b787733afb1b294058967607e192a09e` |
| `refs/heads/task/r2-sdk-url-assets` | `2d6143ff3e1332e15138fe7cfd735656fcadedf1` |
| `refs/heads/task/r2-source-selection-types` | `8e8d3c0d3f45af1f5cfec0b6a2e7102bbb3cd657` |
| `refs/heads/task/r2-source-url` | `c435c0ca948e22729cab182006f0b94b1c60b361` |
| `refs/heads/task/r2-tools-review` | `856b83fa01ec47782105713fe9d5aca090ada81c` |
| `refs/heads/task/r2-validator-prefork` | `44bd47ba783807bdd4444dff1187debba116732d` |
| `refs/heads/task/shadcn-build-layouts` | `354f5b370b64dd0fd7211c5adf002a1c436be020` |
| `refs/heads/task/shadcn-list-layouts` | `bf53a64f68bc0f73efb62827a2115c5da39c45b3` |
| `refs/heads/task/shadcn-visual-regression` | `235397003ce177526c51b7d87417d8857f77b0a1` |
| `refs/heads/task/visual-rollout-builds` | `1129c8062b70ee7e0ba00c469b7c4c54bf97f9a5` |
| `refs/heads/task/visual-rollout-management` | `7ae5a61e1ebeb4eca8d798588fce499f2ddb4936` |
| `refs/heads/task/visual-rollout-materials` | `06f5c4ac0a1096966b7b26ea8276a50d6bf817e9` |
| `refs/remotes/origin/HEAD` | `4f571406cab2690d04a2be16d18df042ef70ffe7` |
| `refs/remotes/origin/dependabot/bun/npm-packages-c31f419f85` | `131b66bb018e0c272f78029deda7888607f036d7` |
| `refs/remotes/origin/dependabot/docker/frontend/docker-7aa794ebb2` | `7f001795b3119f202b8083abf598e00c2d71fb15` |
| `refs/remotes/origin/dependabot/uv/psycopg-binary--gte-3.3.5-and-lt-4.0.0` | `c4b8d23a768ec87c033b63c2bd06571704feae56` |
| `refs/remotes/origin/dependabot/uv/python-packages-b5e8b4702e` | `fa84909f67dc40733664cc55f673dabc80143893` |
| `refs/remotes/origin/feat/platform-implementation` | `4f571406cab2690d04a2be16d18df042ef70ffe7` |
| `refs/remotes/origin/feat/r2-batch-video-upload` | `46f4f8c03a2e77763d5a4f31c3a640b69a97eb28` |
| `refs/remotes/origin/feat/shadcn-official-alignment` | `4c732a20937a00ef18da04dde17d861baf9456d4` |
| `refs/remotes/origin/feat/strategy-visual-refinement` | `601c181d1cca7b62fcbd8248cd5db8be87f96d07` |
| `refs/remotes/origin/feat/workspace-visual-rollout` | `96c7b14d6010365e8f511a9ffcfd48aa2e579617` |
| `refs/remotes/origin/task/p02-discovery` | `b9c4f0a26edb0325a997ea9dcbdeb4734bbc18fd` |
