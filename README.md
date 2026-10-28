# Grok WoW Overlay

A floating AI assistant for **World of Warcraft** — works with WoW Forever,
Retail, Classic, and Classic Era. Press a hotkey to ask Grok (xAI) about quests,
professions, zones, or anything else — it automatically knows your
character, location, and quest log, and can look at your screen when that's
more useful than a text description.

## Download

**[⬇ Download the installer](https://github.com/BabyGrug/claude-wow-overlay/releases/latest/download/ClaudeWowOverlaySetup.exe)**

Run it and follow the setup wizard — a couple of minutes, mostly automatic.

### Requirements

- **A SuperGrok or X Premium+ subscription** and xAI's official **Grok
  Build** app (the `grok` command). The overlay runs it on your PC, signed
  in with your subscription -- the same way the old Claude version ran the
  Claude app -- so there's no separate API bill and nothing you ask passes
  through anyone else. The setup wizard does this for you; by hand it's:
  1. In PowerShell: `irm https://x.ai/cli/install.ps1 | iex`
     ([xAI's install instructions](https://docs.x.ai/build/overview))
  2. Then `grok login` and sign in in your browser with the account that has
     the subscription.

  Grok Build is in beta; xAI may limit how much you can use it per your plan.
- **Optional alternative: an xAI API key** (pay-as-you-go, billed separately
  by xAI). Only used if Grok Build isn't installed. Create one at
  [console.x.ai](https://console.x.ai) (API Keys; needs credits), then choose
  "Use an API key instead" in the setup wizard or run
  `setx XAI_API_KEY "xai-..."` and restart the overlay. To force one backend,
  set `"backend"` to `"cli"` or `"api"` in
  `%LOCALAPPDATA%\ClaudeWowOverlay\settings.json` (default `"auto"`).
- World of Warcraft (any of Forever, Retail, Classic, or Classic Era). The
  companion addon (character/location/quest awareness) is optional but
  recommended -- the installer offers to set it up automatically in whichever
  install(s) you pick.

### A note on security warnings

This isn't code-signed (a real certificate costs real money, not worth it for
a small hobby project), so Windows SmartScreen may call it "unknown
publisher," and some antivirus tools flag unsigned packaged Python apps as
suspicious on first download. That's a known false-positive pattern for this
kind of app, not an actual problem — the full source is right here if you
want to check it yourself.

## Features

- Floating, draggable, resizable box — `Ctrl+Shift+Space` to show/hide
- Knows your character, location, and quest log automatically (via the
  companion WoW addon)
- Screenshot analysis (`Ctrl+Shift+S`) — ask about what's on your screen,
  like comparing quest reward options, without touching the mouse (so a
  tooltip you're hovering over doesn't disappear)
- Marks a location straight onto your in-game map from a chat answer
  (`/grokmark`)
- Remembers settled facts about your character (stat priority, talent
  builds, rotation, profession milestones) so it doesn't need to re-research
  the same question every time, and knows to double-check level-sensitive
  ones as you level up
- Checks a ranked hierarchy of sources (official Blizzard > Wowhead > Icy
  Veins > everything else) for whichever WoW version you're actually
  playing, falling back to general Classic/Vanilla knowledge on WoW Forever
  specifically if nothing Forever-specific turns up — and always tells you
  when it's doing that
- Uses Grok with web search; effort (Low/Medium/High) is adjustable in
  Settings
- Checks for updates on startup and shows a button when a new version is out

## Updating

The app checks for a newer release on startup. If one's out, a small
"Update" button appears in the title bar — click it and it downloads and
runs the latest installer for you.

## For developers

`overlay.py` is the app (Tkinter + the local `grok` CLI in headless
`streaming-messages-json` mode, or `https://api.x.ai/v1/responses` with an
API key), `installer.py` the setup wizard, and
`payload/ClaudeContext/` the WoW addon. See `AGENTS.md` for architecture
notes. Some internal names still say "Claude" (repo, exe, addon folder) on
purpose, so existing installs keep working.
