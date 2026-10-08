# Cloud

A Cloud server is **optional**: a rented machine for heavy work. *Cloud* is a role, not a
machine — the address, provider, operating system and hardware behind it may change every time
you rent one.

## Adding or replacing it

**Servers → Add Cloud server** (or **Replace Cloud server**). Replacement is transactional:

```
reachability → fingerprint → sign-in → health check → activate
```

If any step fails, **the previous Cloud server stays active** and nothing changes. You confirm
the server's fingerprint against your provider's console before anything is trusted.

The server must accept your SSH key: add your public key when you create the machine at your
provider.

## Sending a project

Cloud receives **only the projects you explicitly choose** — never the whole work folder.

**Servers → Send a project to Cloud → Review and send…** shows, before anything moves:

| | |
|---|---|
| Project | the folder you picked |
| Files and size | what will be transferred |
| Goes to | the folder on the server |
| Not sent | Git internals, caches, assistant state |
| Credential files inside | for example `.env` — listed so you decide knowingly |

A large transfer, or one containing credential files, needs an explicit confirmation.
A transfer can be repeated; it continues where it stopped. **Bring results back** copies
changed files to your computer, keeping a backup of anything it replaces.

Sending uses `rsync` over SSH. Windows does not include `rsync`; the feature is reported as
*Unavailable* there until one is installed.

More: [advanced cloud notes](advanced/cli-cloud.md).
