# Agent instructions — solar monitor

See `README.md` for this repository's purpose, setup and usage.

## Shared guidance

Before planning, proposing, reviewing, delegating or implementing changes,
read the shared engineering guidance in `bardphysicslab/bardbox`. Your tool
does not load it automatically.

1. Resolve `main` once per task.
   - With a local bardbox clone, usually `../bardbox` beside this repository:
     run `git -C ../bardbox fetch origin main`, then
     `git -C ../bardbox rev-parse origin/main`. If the fetch fails, use the
     existing `origin/main` and say it may be stale.
   - Without a clone, run
     `gh api repos/bardphysicslab/bardbox/commits/main --jq .sha`.
2. Read these files at that SHA:
   - `docs/engineering/README.md`
   - `docs/engineering/development-workflow.md`
   - `docs/engineering/agent-practice.md`
   - `docs/engineering/checkouts-and-worktrees.md`

   With the clone, use `git -C ../bardbox show <sha>:docs/engineering/<file>`.
   Without it, use
   `gh api "repos/bardphysicslab/bardbox/contents/docs/engineering/<file>?ref=<sha>" -H "Accept: application/vnd.github.raw"`.
3. Record `Shared guidance: bardphysicslab/bardbox@<sha>` once in the task's
   durable evidence. If a review or proposal produces no such artifact, state
   it once in your response.

Copies (desktop files, chat project sources, memory) do not substitute for
the resolved SHA. If a required file cannot be read at the resolved SHA, do
read-only investigation only, and report it. Do not implement, commit, push,
deploy or end checkouts unless the maintainer explicitly says to proceed
without it.

This is a BardBox project: also read bardbox's root `AGENTS.md` and
`ARCHITECTURE.md` at the same SHA, and the detailed standards relevant to the
task.
