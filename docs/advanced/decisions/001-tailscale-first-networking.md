# ADR-001: Tailscale-first networking

Status: accepted · 2026-10-08

## Context

Machines change address, provider and network. The user should say `ssh home`, not an IP.

## Decision

Every managed machine joins one tailnet; MagicDNS names are stored in the inventory and rendered into SSH aliases. Public IPs appear only during cloud enrollment.

## Consequences

Stable names and encrypted paths without port forwarding. Dependency on a third-party coordination service; mitigated by falling back to the public endpoint for cloud and by local work never depending on the tailnet.
