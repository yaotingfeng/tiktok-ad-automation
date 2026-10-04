# Final compatibility fix report

## Scope

The final review found that `backend/app/modules/builds/drafts.py:_materials_page` still dereferenced the removed `StrategyConfig.group_size` field while matching materials for a new strategy. This fix is limited to that draft material persistence path and its regression coverage.

## Fix

- Removed the unused strategy-version/config lookup from `_materials_page`.
- Persisted automatically matched materials as one ordered source package: `group_no=1`, with `position` increasing from `1` for each newly matched material.
- Kept content-key deduplication and pagination behavior unchanged.
- Updated the existing pagination regression and added a focused new-strategy draft regression asserting the source package order and stable positions.
- Removed the incorrect global `creative_count` versus `max_ads_per_adgroup` comparison. During preview expansion, each actual planned group now checks its final ad count (`base ads × creative_count`) against the frozen per-group limit and records `creative_count_exceeded` when it is over the limit.
- Added the `preview_ad_material_frozen` child-write trigger to the preview migration and removed it during downgrade, matching the existing frozen preview child tables.

Preview planning remains responsible for deriving final ad-group and ad-level allocation from the frozen source package.

## Verification

- `uv run ruff check backend/app/modules/builds/drafts.py backend/tests/modules/builds/test_drafts.py` — passed.
- `uv run python -m compileall -q backend/app/modules/builds/drafts.py backend/tests/modules/builds/test_drafts.py` — passed.
- `uv run python -m compileall -q` on the changed preview validation, preview expansion, migration, draft, and focused test files — passed.
- `uv run pytest --noconftest tests/modules/builds/test_structure_preview.py -q` — 9 passed (offline planner, validation, and migration assertions).
- `uv run pytest --confcutdir=/tmp tests/modules/builds/test_structure_preview.py -q` — blocked before collection because the environment has no dedicated PostgreSQL test `DATABASE_URL`.
- `git diff --check` — passed.
- Focused pytest command was attempted:
  `uv run pytest tests/modules/builds/test_drafts.py -q -k 'new_strategy_draft_keeps_material_match_order_in_source_package or preparation_matches_all_material_pages_once_per_drama'`
  It was blocked before collection because the environment has no dedicated PostgreSQL test `DATABASE_URL`.

No live TikTok, MCP, or advertising writes were made.
