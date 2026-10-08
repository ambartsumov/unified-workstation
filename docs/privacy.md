# Privacy

**The product collects no usage data.** There is no account, no analytics, no telemetry and no
advertising identifier, and no data is sold or shared — there is none to sell.

## Network requests the application makes by itself

| Request | When | What is sent |
|---|---|---|
| Update check | once a day, if *Check for updates daily* is on | a plain download of the public update manifest from the release page; nothing about you |
| Connections between your computers | when sync or keyboard sharing is on | your files and input, encrypted, only to computers you paired |
| Connections to your servers | when you set up Home or Cloud | SSH to the addresses you entered |

Switch the update check off under **Settings → Updates**. Everything else exists only because
you set it up.

## Telemetry setting

**Settings → Privacy** shows *Usage data collection: Off* and it can not be switched on,
because no collection exists. If opt-in diagnostics are ever added, they will be off by
default, described in full before you decide, and announced in the changelog.

## What stays on your computer

Settings, the list of paired computers, the event log and snapshots — in the application's
own folders (**Settings → Advanced** shows where). Passwords stay in the operating system's
credential store.

## Support bundles

Created only when you ask (**Health → Export support bundle**), saved as a file you can read
before sending, with names and addresses replaced by placeholders. Nothing is uploaded by the
application.

## Third parties

Syncthing, Deskflow and Tailscale are separate products with their own policies. The
application configures Syncthing with usage reporting, crash reporting, global discovery and
relays switched off.
