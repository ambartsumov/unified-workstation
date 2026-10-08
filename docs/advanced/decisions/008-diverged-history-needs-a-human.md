# ADR-008: Diverged history needs a human

Status: accepted (2026-10-08). Supersedes the "merge by default" part of ADR-002.

## Context
0.1 merged diverged history automatically when the merge had no textual conflicts. The V1
contract requires that two independently committed sides end in `DIVERGED` with nothing
overwritten, and ranks predictability above cleverness.

## Decision
`sync.policy` defaults to `manual`. On divergence SUW creates one recovery branch
(`suw/recovery/<device>/<timestamp>`), notifies once and waits. `suw project recovery`
offers merge / rebase (both aborted on conflict) as explicit, single-command actions.
`merge` and `rebase` remain available as an opt-in policy. To make divergence rare, each
cycle first fast-forwards (also over a dirty tree when no file overlaps), then checkpoints.

## Consequences
Two machines edited while both were offline need one command from the user. In exchange no
merge commit ever appears that the user did not ask for.
