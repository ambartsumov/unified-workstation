# ADR-003: Never file-sync `.git`

Status: accepted · 2026-10-08

## Context

File synchronisers copying `.git` between machines corrupt repositories under concurrent use.

## Decision

Git is the only transport for repositories. Syncthing is optional and limited to non-Git folders, with an explicit ignore list.

## Consequences

A whole class of repository corruption is ruled out. Uncommitted work does not travel until checkpointed; opt-in automatic checkpoints cover that.
