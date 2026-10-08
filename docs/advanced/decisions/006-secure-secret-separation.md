# ADR-006: Secrets live in the OS keychain

Status: accepted · 2026-10-08

## Context

Configuration is synced through Git and must be shareable.

## Decision

Config may contain `keychain://name` references only. Values are stored via Secret Service on Linux and Keychain on macOS and are re-issued per machine rather than copied.

## Consequences

The shared config repo can be read by anyone with repo access without leaking credentials. A new machine needs its secrets entered once.
