---
description: Interactive setup for relay's optional features (terminal decoration, summary directory).
---

# Setup Relay

Interactive setup for relay's optional features. Detects the user's environment and configures settings with confirmation at each step.

## Steps

1. **Welcome and confirm.** Ask the user:

   > Relay setup can configure these optional features:
   >
   > 1. **Terminal decoration** — colored backgrounds and tab titles per workstream
   > 2. **Summary report directory** — where `/relay:summarize` writes reports (e.g., an Obsidian vault)
   >
   > Would you like guided setup, or would you prefer the documentation? (See the "Setup" section in the relay README.)

   If the user wants docs, point them to the README and stop.

2. **Detect environment.** Run these checks and report what you find:
   - **Terminal:** Check `$WT_SESSION` (Windows Terminal), `$ITERM_SESSION_ID` (iTerm2), `$TERM_PROGRAM` (ghostty, Apple_Terminal, others)
   - **Shell:** Check `$SHELL` (zsh, bash, etc.)
   - **Existing statusLine:** Read `~/.claude/settings.json` and check if `statusLine` is already configured
   - **Existing relay config:** Check if `~/.config/relay/relay.json` exists and what it contains

   Show the user what you found before proceeding.

3. **Terminal decoration preferences.** Ask the user which visual indicators they want:

   - **Background color** — changes the terminal background per workstream (auto-generated or custom)
   - **Tab title** — sets the terminal tab title to show workstream name, directory, and branch

   Options: both (recommended), color only, title only, or neither. If neither, skip to step 5.

   Save their preferences to `~/.config/relay/relay.json`:
   ```bash
   mkdir -p ~/.config/relay
   # Merge terminal_color and terminal_title into existing config
   jq --argjson color <true|false> --argjson title <true|false> \
     '. + {"terminal_color": $color, "terminal_title": $title}' \
     ~/.config/relay/relay.json > /tmp/relay-config.json && \
     cat /tmp/relay-config.json > ~/.config/relay/relay.json
   ```
   If the file doesn't exist yet, create it with `jq -n`.

4. **Terminal decoration setup.** Only if the user enabled color and/or title:

   a. **Check for existing statusLine.** If `~/.claude/settings.json` already has a `statusLine` config:
      - Show the user what's currently configured
      - Ask: "You already have a statusLine configured. Would you like to **replace** it with relay's, **skip** this step, or **see the docs** so you can merge them manually?"
      - If skip, move on. If docs, point to README.

   b. **Copy statusline script.** Copy the script from the plugin into the user's config:
      ```bash
      cp "${CLAUDE_PLUGIN_ROOT}/scripts/statusline.sh" ~/.claude/statusline-command.sh
      ```
      Note: `CLAUDE_PLUGIN_ROOT` is available because this runs within Claude Code's plugin context. The script is copied to a fixed path so `settings.json` can reference it without depending on the plugin cache path.

   c. **Apply settings.** With user confirmation, update `~/.claude/settings.json` to add/set:
      ```json
      {
        "env": {
          "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"
        },
        "statusLine": {
          "type": "command",
          "command": "bash ~/.claude/statusline-command.sh"
        }
      }
      ```
      Use `jq` to merge these into the existing settings file (don't clobber other settings). If the file doesn't exist, create it with just these keys.

   d. **Shell-specific guidance.** If zsh and title is enabled:
      > Add `export DISABLE_AUTO_TITLE="true"` to your `~/.zshrc` to prevent your shell from overriding the tab title.

   e. **Color customization.** If color is enabled, mention to the user:
      > Each workstream gets an auto-generated background color based on its name. To override it, you can ask Claude to "change [workstream]'s background color to [hex]" — it will use the `update_workstream` tool.
      >
      > You can also set it directly in `~/.config/relay/workstreams.json`:
      > ```json
      > "my-workstream": {
      >   "status": "active",
      >   "color": "#0d1a2d",
      >   ...
      > }
      > ```
      > Use any dark hex color — it needs to be readable with light text.

   f. **Terminal-specific guidance.** Based on the detected terminal:
      - **Windows Terminal** (`$WT_SESSION` set): "In Windows Terminal settings, go to your profile (e.g., Ubuntu) → Advanced/Terminal Emulation → turn **off** 'Suppress Title Changes'."
      - **iTerm2** (`$ITERM_SESSION_ID` set): "In iTerm2, go to Settings → Profiles → General → Title and change the dropdown to **Session Name** (or **Session Name (Job)** if you want both). Also ensure **Applications in terminal may change the title** is checked (same section). Relay uses AppleScript to set the tab title on iTerm2."
      - **Ghostty** (`$TERM_PROGRAM=ghostty`): "Ghostty supports the standard escape sequences natively. No extra configuration needed."
      - **Could not detect / Other:** "Relay uses standard OSC escape sequences (OSC 0 for title, OSC 11 for background color) which are supported by most modern terminals. If the tab title or background color doesn't change, check your terminal's documentation for settings like 'allow title changes' or 'suppress title changes'. You can test manually with: `printf '\\e]0;test\\a'` (title) and `printf '\\e]11;#0d1a2d\\a'` (background)."

5. **Summary report directory.** Ask the user:

   > Where should `/relay:summarize` write report files?
   >
   > - Current setting: `<show current value from ~/.config/relay/relay.json, or "not set (default: ~/.local/share/relay/summaries)">`
   > - Common choices: an Obsidian vault folder, a project docs folder, or the default
   >
   > Enter a path, or press Enter to keep the current setting.

   If the user provides a path, merge into `~/.config/relay/relay.json`:
   ```bash
   mkdir -p ~/.config/relay
   jq --arg dir "<path>" '. + {"summary_dir": $dir}' ~/.config/relay/relay.json > /tmp/relay-config.json && cat /tmp/relay-config.json > ~/.config/relay/relay.json
   ```
   If the file doesn't exist, create it with `jq -n`.

6. **Summary.** Show what was configured and any manual steps the user still needs to take. Mention that:
   - Terminal decoration takes effect on the next Claude Code session
   - Re-run `/relay:setup-relay` after plugin updates to get the latest statusline script
   - Preferences can be changed later by re-running `/relay:setup-relay` or editing `~/.config/relay/relay.json`
