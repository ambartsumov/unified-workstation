# Workstations and pairing

A *workstation* is a computer running the application. Paired workstations trust each other;
nothing else on your network is trusted, ever.

## Pairing two computers

1. On computer A: **Workstations → Add Workstation**. A pairing code appears.
2. On computer B: **Workstations → Pair with a code**, paste A's code.
3. B shows what will happen — how many files each side has, and whether folders will be
   merged — and a **six-digit confirmation number**.
4. Enter B's code on A the same way. A shows the same six digits.
5. Compare the numbers. If they match, confirm on both. If they differ, cancel.

The pairing code is **not a secret**: it carries only public identities and where the computer
can be reached. The confirmation number is what proves that the two computers in front of you
are the ones exchanging codes.

## When both folders already contain files

Nothing is deleted. Files that exist on one side are copied to the other. A file that exists
on both sides with different content is kept twice (a conflict copy) so you can choose. The
assistant asks you to confirm the merge explicitly.

## Managing workstations

The **Workstations** page lists this computer and every paired one: online or offline,
operating system, version, how far in sync it is, when it was last seen.

- **Rename** — lowercase letters, digits and dashes.
- **Unpair** — the computers stop syncing and sharing input. Files stay on both.

## What this computer can do

The same page shows a capability table for *this* computer: each feature is **Supported**,
**Partially supported**, **Needs permission**, **Not installed** or **Unavailable**, with the
reason. See [Supported platforms](supported-platforms.md).

## Without a network in common

Computers on the same home or office network find each other directly. To pair computers that
are never on the same network, both need a private network such as Tailscale; the pairing code
then carries the private-network name.

Next: [Keyboard and mouse](peripherals.md)
