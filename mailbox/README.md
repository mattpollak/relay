# relay mailbox

Optional, self-hosted store for [relay](../README.md)'s multi-machine sync — an alternative to a shared folder
for machines that aren't always on the same network.

- **Sees nothing:** everything relay uploads is already compressed, encrypted to your approved machines and
  signed. The mailbox can't read or alter it.
- **Enforces the folder rules:** one bearer token per machine (stored as a hash). A token is bound to one relay
  host ID and can only create or delete files whose names start with that ID. Files are create-only.
  Without a token you can't even list files.
- **No dependencies:** Python standard library. Data is a directory of files plus `tokens.json`.

Run it behind something that provides TLS and limits who can connect — the example uses a
[Tailscale](https://tailscale.com) sidecar in kernel networking mode, so the mailbox is reachable only from
your tailnet over HTTPS (`tailscale serve` on 443; the mailbox itself listens on 127.0.0.1). Restrict the
tailnet ACL to your own devices on `tag:relay-mailbox:443`.

## Run

See `docker-compose.example.yml`. Without Docker:

```bash
python -m relay_mailbox.server serve --data ./data --host 127.0.0.1 --port 8484
```

## Tokens (one per machine)

```bash
python -m relay_mailbox.server add-token --data ./data laptop     # prints the token once
python -m relay_mailbox.server list-tokens --data ./data
python -m relay_mailbox.server revoke-token --data ./data laptop  # lost machine: also revoke it in relay
```

A revoked token's host ID stays reserved, so a new token can't take over that machine's files. To give the
same machine access again, issue one bound to its host ID: `add-token laptop2 --host <host_id>`.

Keep tokens in a password manager. On each machine, put its token in `~/.config/relay/sync.token`
(mode 0600) without pasting it into a chat, e.g.
`op read op://Vault/relay-mailbox-laptop/credential > ~/.config/relay/sync.token && chmod 600 ~/.config/relay/sync.token`,
then run `/relay:sync setup` with backend `http`. Alternatively set `token_command` so relay runs the
password manager each time it syncs.

## API

All under `/v1` with `Authorization: Bearer <token>`: `GET whoami`, `POST bind {"host_id"}`, `GET files`,
`GET|PUT|DELETE files/<name>`. Plus unauthenticated `GET /healthz`.
