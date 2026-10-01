# Agent instructions

Read `ARCHITECTURE.md` before changing load control. Verify controller changes
with an injected fake driver and fake `sleep_fn` / `monotonic_fn` first.
Energize the load only when the task authorizes hardware operation, and record
the scenario (setup, limits, expected states, observed result).

Before a refactor or an architectural assessment, read the "Architectural
self-check" and "Refactoring with little test coverage" sections of
https://github.com/bardphysicslab/bardbox/blob/main/AGENTS.md.
