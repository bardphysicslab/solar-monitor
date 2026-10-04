# Agent instructions — solar monitor

See `README.md` for this repository's purpose, setup and usage.

## Shared guidance

Before planning, proposing, reviewing, delegating or implementing changes,
read the shared engineering guidance in
`bardphysicslab/engineering-standards`. Your tool does not load it
automatically.

1. Resolve `main` once per task.
   - With a local clone, usually `../engineering-standards` beside this
     repository: run `git -C ../engineering-standards fetch origin main`,
     then `git -C ../engineering-standards rev-parse origin/main`. If the
     fetch fails, use the existing `origin/main` and say it may be stale.
   - Without a clone, run
     `gh api repos/bardphysicslab/engineering-standards/commits/main --jq .sha`.
2. Read these files at that SHA:
   - `README.md`
   - `development-workflow.md`
   - `agent-practice.md`
   - `checkouts-and-worktrees.md`
   - `agent-coordination.md`

   With the clone, use `git -C ../engineering-standards show <sha>:<file>`.
   Without it, use
   `gh api "repos/bardphysicslab/engineering-standards/contents/<file>?ref=<sha>" -H "Accept: application/vnd.github.raw"`.
3. Record `Shared guidance: bardphysicslab/engineering-standards@<sha>` once
   in the task's durable evidence. If a review or proposal produces no such
   artifact, state it once in your response. A task that already recorded
   `bardphysicslab/bardbox@<sha>` keeps that governing commit.

Copies (desktop files, chat project sources, memory) do not substitute for
the resolved SHA. If a required file cannot be read at the resolved SHA, do
read-only investigation only, and report it. Do not implement, commit, push,
deploy or end checkouts unless the maintainer explicitly says to proceed
without it.

This is a BardBox project: also read bardbox's root `AGENTS.md` and
`ARCHITECTURE.md` at one resolved `main` commit of `bardphysicslab/bardbox`,
and the detailed standards relevant to the task.
