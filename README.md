# Claude WoW Overlay

A floating AI assistant for **World of Warcraft** — works with WoW Forever,
Retail, Classic, and Classic Era. Press a hotkey to ask Claude about quests,
professions, zones, or anything else — it automatically knows your
character, location, and quest log, and can look at your screen when that's
more useful than a text description.

## Download

**[⬇ Download the installer](https://github.com/BabyGrug/claude-wow-overlay/releases/latest/download/ClaudeWowOverlaySetup.exe)**

Run it and follow the setup wizard — a couple of minutes, mostly automatic.

### Requirements

- **The official Claude desktop app**, already installed (get it from
  claude.ai). This overlay runs entirely on top of its bundled CLI and your
  own login — that's also *why* nothing you ask ever passes through anyone
  else. It goes straight from your machine to your own Claude account.
- **Your own Claude subscription.** This isn't a shared account — everyone
  who installs it authenticates their own.
- World of Warcraft (any of Forever, Retail, Classic, or Classic Era). The
  companion addon (character/location/quest awareness) is optional but
  recommended — the installer offers to set it up automatically in whichever
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
- Remembers settled facts about your character (stat priority, talent
  builds, rotation, profession milestones) so it doesn't need to re-research
  the same question every time, and knows to double-check level-sensitive
  ones as you level up
- Checks a ranked hierarchy of sources (official Blizzard > Wowhead > Icy
  Veins > everything else) for whichever WoW version you're actually
  playing, falling back to general Classic/Vanilla knowledge on WoW Forever
  specifically if nothing Forever-specific turns up — and always tells you
  when it's doing that
- Checks for updates on startup and shows a button when a new version is out

## Updating

The app checks for a newer release on startup. If one's out, a small
"Update" button appears in the title bar — click it and it downloads and
runs the latest installer for you.
