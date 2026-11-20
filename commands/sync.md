---
description: Optional multi-machine sync — status, setup, sync now, approve/revoke a machine, or turn it off.
argument-hint: "[status | setup | now | approve <machine> | revoke <machine> | disable [--forget]]"
---

# Sync

Multi-machine sync is **optional and off by default**. Until it's set up, relay is local-only: no keys, no shared folder, nothing extra runs.

With sync on, relay shares workstreams, state files, ideas, session hints and tags with your other machines through a **shared folder** (OneDrive, Syncthing, a NAS share) or a self-hosted **relay mailbox** (e.g. on a NAS over Tailscale). Everything relay writes is encrypted to your approved machines and signed by the machine that wrote it; the folder's host or the mailbox can't read it.

The subcommand is `$ARGUMENTS` (empty means `status`).

## status (default)

Call `manage_sync(action="status")` and show the result as-is. If it lists a machine **waiting to join**, ask the user whether it's theirs before suggesting `approve`.

## setup

Walk the user through it, one question at a time.

1. **Show the current state.** Call `manage_sync(action="status")` and show it, so the user sees this machine's hostname and any existing settings.

2. **Nickname.** Ask what this machine should be called on the other machines. Offer to keep the hostname shown in the status. Examples: "laptop", "desktop". At most 32 characters.

3. **Folder.** Ask for a folder that all their machines can see, used only by relay. Suggest a new subfolder inside something that already syncs, for example `/mnt/c/Users/<user>/OneDrive/relay-sync` on WSL, `~/OneDrive/relay-sync`, or a Syncthing folder. It's created if its parent exists. Every machine must use the folder that syncs to the same place.
   - The first machine set up in an empty folder **founds** the group.
   - Later machines **ask to join** and get nothing until an existing machine approves them.

   **Or a relay mailbox (`http`)** — for machines that aren't always on the same network. Ask for its URL (`https://…`, e.g. `https://relay-mailbox.<tailnet>.ts.net`). Each machine needs **its own token** from whoever runs the mailbox (`relay-mailbox add-token <machine>`). **Never ask the user to paste the token into the chat** — it would end up in the transcript and the search index. Instead, one of:
   - the user writes it to a file themselves, e.g. `! op read "op://Vault/relay-mailbox-<machine>/credential" > ~/.config/relay/sync.token && chmod 600 ~/.config/relay/sync.token` (the `!` runs it in this session without showing the token), then setup with no token argument; or
   - `token_command="op read op://Vault/relay-mailbox-<machine>/credential"` so relay asks the password manager on every sync (may prompt for approval).

4. **Scope.** Ask what to share:
   - **state** (recommended): workstreams, state files, ideas, session hints, tags, and git snapshots (uncommitted/unpushed work per repo).
   - **state+messages**: also conversation messages, so history search, `get_conversation` and summaries cover every machine (results say which machine). Transcripts can contain secrets, which is why this is a separate choice; if one leaks into history, `/relay:redact` removes it from every machine. The first push sends this machine's whole indexed history.

5. **Save.** Call:
   ```
   manage_sync(action="setup", backend="folder", folder="<path>", scope="<state|state+messages>", nickname="<name, or \"\" for the hostname>")
   manage_sync(action="setup", backend="http", url="<https url>", [token_command="<cmd>"], scope=..., nickname=...)
   ```
   The first run on a machine may take a moment: it installs relay's encryption library (PyNaCl) into relay's own environment.
   If it returns `{"error": ...}`, explain the problem and ask for a corrected value. Don't retry with a guessed value.

   **Joining an existing folder:** the first call returns the folder's **founder fingerprint** instead of joining, and nothing is written or sent. Ask the user to run `/relay:sync status` on a machine that's already set up and read its **Founder** fingerprint. Only if the user says they match, call setup again with the same arguments plus `trust_fingerprint="<the fingerprint the user confirmed>"`. **Never pass the fingerprint from the tool's own output without the user confirming it** — if they don't match, someone else created that folder; tell the user not to join it.

6. **Next steps.** Show the tool's message. Then:
   - **Founded:** tell the user to run `/relay:sync setup` on their other machine with the same folder (once the folder has synced there), then come back here and run `/relay:sync approve <name>`.
   - **Waiting for approval:** show this machine's **fingerprint** and tell the user to run `/relay:sync approve <this machine's name>` on a machine that's already set up, checking that the fingerprint shown there is the same. Until it's approved, this machine neither sends nor receives anything.

## now

`manage_sync(action="now")` pulls then pushes immediately — useful before leaving the house or switching machines. Show the result. Syncing also happens on its own at session start, after saves, before compaction and at session end.

## approve <machine>

1. Call `manage_sync(action="status")` and find the machine under **Waiting to join**.
2. Show its name, hostname and **fingerprint**, and ask the user to confirm it matches the fingerprint shown on that machine. **Don't approve without that confirmation** — anyone who can write to the shared folder can ask to join.
3. Call `manage_sync(action="approve", machine="<name, host ID, or fingerprint>")`. If two waiting machines share a name, use the fingerprint.

## revoke <machine>

Confirm with the user first, then `manage_sync(action="revoke", machine="<name or host ID>")`. From then on nothing new is encrypted to that machine and anything it writes is ignored. It keeps what it already synced. If the machine was lost or stolen, also remind the user to remove its access to the shared folder.

## disable

- `/relay:sync disable` → `manage_sync(action="disable")`. Sync stops; the settings are kept so it's quick to turn back on.
- `/relay:sync disable --forget` → confirm with the user first, then `manage_sync(action="disable", forget=True)`. This removes the sync settings.

Either way, all local data (workstreams, history, the event log) stays on this machine. Show the tool's message.

## Conflicts and work on other machines

- If the same state file was edited on two machines from the same version, the newer edit is kept and the other version is saved next to it as `<file>.conflict-<machine>-<time>.md`. Help the user merge what they need into the main file, then delete the conflict file.
- Notices like "work on desk not visible here" describe uncommitted or unpushed work on another machine. When one says a `git fetch` is needed, **offer** to run it — never run `git fetch` without the user agreeing.
