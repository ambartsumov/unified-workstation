# ADR-005: Cloud is a role, not a machine

Status: accepted · 2026-10-08

## Context

Rented servers change IP, provider and hardware constantly.

## Decision

The inventory holds a `cloud` role pointing at the current node; nodes have deterministic labels, pinned public host keys and recorded hardware. Replacement moves the pointer and retires the old node.

## Consequences

`ssh cloud` survives every replacement with no known_hosts conflicts and no relaxed host checking. One node at a time; multi-node pools are future work.
