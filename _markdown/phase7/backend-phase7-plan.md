# Backend plan — Phase 7: sandbox / workflow draft handling (botchain-ai)

## Read first
1. `AGENTS.md` — settled decisions, folder structure, non-negotiable DB rules.
2. `_markdown/backend-setup-qa.md` **Q8** — the Railway ephemeral-disk constraint
   this phase exists to respect.
3. `_markdown/python-fastapi-backendchecklist.md` Phase 7 — the checklist item.

If anything here risks diverging from AGENTS.md, **stop and ask** instead of
deciding silently.

## What the checklist demands
> Don't rely on `SANDBOX_DIR` surviving between requests or replicas. For now:
> single Railway replica, sandbox as scratch space only, final workflow JSON
> persisted to Postgres (in `Message.meta`) at the end of each turn.

**Verification gate:** the workflow survives the request via DB, not disk.

## Current state (pre-existing, verified)
Most of the phase was already in place implicitly; Phase 7 closed the remaining
gap and pinned the gate with tests.

- Per-turn sandbox already existed: `build_node` created
  `tempfile.mkdtemp(prefix="botchain-build-")` and passed it to
  `_make_write_json_tool` (path-escape guarded) in `services/agent.py`.
- The finished workflow is **captured from the tool args**, not read back from
  disk (`captured_workflow = content` in the build loop), so the sandbox is
  genuinely scratch — deletion can never lose work.
- Final workflow persisted to `Message.meta` (`workflow_json` + `workflow_name`)
  on both `valid` and `failed_after_retries`/`build_failed` terminal states
  (`_run_meta` in `api/v1/messages.py`). An `Attachment` row + `/…/attachments/
  {id}/download` route serve it from `Message.meta`.
- No `SANDBOX_DIR` anywhere — code never referenced a persistent path.

## Change made
`services/agent.py` `build_node`: replaced the bare `tempfile.mkdtemp` (leaked a
dir per build) with `tempfile.TemporaryDirectory(prefix="botchain-build-")` as a
context manager. The build loop moved into a small inner helper
`_run_build_loop(...)`; the sandbox dir is now guaranteed torn down when the loop
exits — including on tool-loop exhaustion or an in-loop exception. The write tool
and its escape guard are unchanged.

## Kept decisions (recorded, not re-litigated)
- **DB-backed delivery, no object storage yet.** Q8's single-replica/ephemeral
  constraint makes serving attachments from `Message.meta` correct; S3/R2 object
  storage is deferred to the Phase 9 infra milestone (the Phase-5 follow-up note).
- **Sandbox is scratch-only.** The final JSON never depends on disk; disk is
  only a buffer for the `write_json_file` tool contract.
- **No schema change, no Alembic migration, no new dependency** — Python stdlib
  `tempfile` only.

## Verification
- `tests/test_phase7_sandbox.py` (8 tests):
  - `download_attachment` serves JSON from `Message.meta` with the right
    `Content-Type`/`Content-Disposition` and never touches disk; 404s for
    missing message, missing attachment, and missing `workflow_json`.
  - `build_node`'s sandbox dir is removed after the build returns.
  - `write_json_file` writes inside the sandbox, rejects path escapes, and
    rejects non-JSON content.
- `uv run pytest` — 95 green (87 prior + 8 new), ruff clean.