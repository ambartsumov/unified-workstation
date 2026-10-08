# ADR-002: GitHub as the durable code authority

Status: accepted · 2026-10-08

## Context

Two laptops must converge without being online together.

## Decision

Each workstation keeps an independent clone and reconciles against GitHub. No direct laptop-to-laptop code sync.

## Consequences

Offline-tolerant and auditable; only Git objects move. GitHub outages delay convergence but never block local work. Binaries and operational state need separate storage.
