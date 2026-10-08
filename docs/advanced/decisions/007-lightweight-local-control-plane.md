# ADR-007: Standard-library Python, one daemon, agentless servers

Status: accepted · 2026-10-08

## Context

The system must run on an old Intel Mac and stay maintainable by one person.

## Decision

Python 3.11+ standard library only; a single asyncio daemon with a 0600 Unix socket; servers are observed by a read-only POSIX script over SSH instead of an installed agent; repository changes are detected by a 30 s `git status` poll.

## Consequences

Nothing to build or install beyond one interpreter; identical behaviour on both OSes. Polling is slightly less immediate than file events and is isolated behind `Debounce` for later replacement.
