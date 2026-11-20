---
description: Remove text (e.g. a leaked secret) from relay's history — and from every synced machine.
argument-hint: "<text to remove>"
---

# Redact

Removes text such as a leaked password or token from relay's search index, session hints, event log and its own files, replacing it with `[redacted]`. With multi-machine sync on, the other machines apply the same redaction on their next sync and the shared folder's old encrypted files are rewritten. The text itself is never sent to the other machines.

The text is `$ARGUMENTS`. If it's empty, ask the user what to redact. Don't echo a secret back more than necessary.

1. **Dry run.** Call `redact_history(text="<text>")` and show how many messages and log entries contain it. If there are none, say so and stop.
2. **Confirm.** Ask the user to confirm. Redaction can't be undone.
3. **Redact.** Call `redact_history(text="<text>", confirm=True)` and show the result.
   relay remembers a salted fingerprint of the text (not the text), so if it shows up again in newly indexed messages — including this command line, which is in this session's transcript — it's scrubbed before it's searchable or shared, on every synced machine.
4. **Raw transcripts.** If the result lists Claude Code transcript files (`~/.claude/projects/...jsonl`) that still contain the text, tell the user. relay doesn't modify them; they're Claude Code's own files, on this machine only. If the secret was real, also rotate it: redaction limits where it's stored, it doesn't make it secret again.
