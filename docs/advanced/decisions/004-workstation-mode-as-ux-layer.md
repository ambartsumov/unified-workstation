# ADR-004: Workstation Mode is a UX layer

Status: accepted · 2026-10-08

## Context

Many 'modes' (AI, DevOps, Cloud…) fragment the experience and tempt coupling services to UI state.

## Decision

Exactly two modes. Switching changes workspaces, dock, wallpaper and launched apps; the daemon, network and sync run identically in both.

## Consequences

Toggling is fast, safe and idempotent. Desktop settings are captured on entry and restored on exit, with transitional states for crash recovery.
