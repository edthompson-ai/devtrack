# Handoff: the one task left (live GitHub sync)

Everything else is done and verified (see the README's Evidence section). This step
needs GitHub access, which the build environment didn't have.

## Goal

Run `devtrack sync` against one public repository, twice, and save the logs as
evidence that ingestion handles real data: rate limits, retries, and an idempotent
rerun.

## Steps

```bash
pip install -e ".[dev]"
export DATABASE_URL=sqlite:///live.db
export GITHUB_TOKEN=...            # fine-grained, read-only: Contents, Pull requests, Actions
devtrack init

# First sync: one repo, capped so it stays within rate limits.
devtrack sync --repo pallets/click --max-items 100 2>&1 | tee docs/evidence/live-sync-1.txt

# Second sync straight after: should fetch only what changed and write nothing new.
devtrack sync --repo pallets/click --max-items 100 2>&1 | tee docs/evidence/live-sync-2.txt

devtrack dq 2>&1 | tee docs/evidence/live-sync-dq.txt
```

## What to check in the logs

- First run: `staged.new` > 0, no `.error` counts, and a `requests` count.
- Second run: `staged.new` is 0 or close to it, and no `.written` counts apart from
  records GitHub really changed in between.
- `devtrack dq`: every check passes.
- If GitHub returns 403 or 429, the client waits and retries (logged as a warning);
  the run should still finish.

## Rules

- **Never commit the token** or paste it into any file. It's read from the
  environment only.
- Commit only the three log files, then add a row to the README's Evidence table and
  change the "Live GitHub sync" row under "What was not verified".
