# Changelog

## v1.2.7
- The box now shows itself when you launch the app, instead of starting
  hidden until you press the hotkey. Launching it from the shortcut and
  seeing nothing happen looked exactly like it had failed to start, when it
  was actually running the whole time. (It already did this right after an
  update; now every launch does.) The hotkey and the X still hide/show it
  the same as before.

## v1.2.6
- Fixed every question failing with "[failed to run claude.exe: [WinError 2]
  The system cannot find the file specified]" after the Claude desktop app
  updated itself while the overlay was left running. The overlay looked up
  claude.exe once at startup and kept using that path, but the path
  includes the CLI's version number and the desktop app deletes old version
  folders when it updates. It now re-finds it automatically whenever the
  saved path has gone stale (or was never found at startup), retrying
  briefly in case the app is mid-update, and only shows a plain-language
  message if it genuinely can't be found -- no more raw Windows error text,
  and nothing anyone needs to report or restart to fix.

## v1.2.5
- Fixed the addon showing "Incompatible" and never loading on Retail. The
  guessed Retail interface range from v1.2.1 (110000-110207) was wrong --
  confirmed directly that a real Retail client is on interface 120100, via
  `/run print(select(4, GetBuildInfo()))`. Updated the TOC to the real
  number. If you saw "Incompatible" in the AddOns list before, a `/reload`
  after this update should fix it.

## v1.2.4
- Fixed Claude flatly refusing questions about a different WoW version than
  whichever one your conversation happened to start on -- e.g. asking about
  Retail class balance while the conversation was originally created while
  playing WoW Forever got a hard "that's outside what I search for Forever"
  refusal, even though it was a plain, generally-answerable question. New
  conversations now always have both the Forever-specific and generic
  search-source lists available, and decide which applies per-question
  instead of locking the whole conversation to one version. **Only affects
  conversations created from now on** -- an existing/resumed conversation
  keeps whatever got baked in when it was first created (a real limitation
  of how session prompts work, not something fixable after the fact); click
  "New" to pick up the fix in an old conversation.

## v1.2.3
- Fixed updates silently never appearing. Two separate problems, both real:
  the app only ever checked for updates once at startup, so anyone who left
  it running for a while would never find out a new version existed no
  matter how long it had been out -- now it also re-checks automatically
  every couple hours. And the check silently treated a genuine failure
  (offline, GitHub down, rate-limited) exactly the same as "you're up to
  date," with zero way to tell the difference -- confirmed directly that
  this is exactly what happened (GitHub's public API is rate-limited to
  60 requests/hour per IP, and enough app relaunches during testing burned
  through it). A failed check now says so.
- Added "Check for updates now" to Settings for an on-demand check instead
  of waiting for the automatic one.

## v1.2.2
- Fixed the box seemingly vanishing after installing an update. It was
  still running the whole time, just relaunching hidden like any other
  normal startup instead of showing itself -- the hotkey brought it back,
  but nothing told you that. It now shows itself automatically the moment
  the update finishes.

## v1.2.1
- The setup wizard now finds *every* WoW install on your machine (WoW
  Forever, Retail, Classic, Classic Era) and lets you check off which ones
  to set the addon up in, instead of only ever picking one automatically.
  Uninstalling and updating both clean up/refresh every install you chose.
- The addon now records which WoW version it's running on and tells Claude
  explicitly, so it can't confidently answer a Retail question off stale
  Classic data (or vice versa) without at least flagging the mismatch.
- Added a defensive legacy quest-log fallback and widened the addon's
  supported interface versions for Classic Era/Classic/Retail.
- **Retail and Classic support is best-effort and not yet verified against
  a real client** (only WoW Forever has been -- repeatedly, and only ever
  by actually testing, not guessing). If something doesn't work right on
  Retail/Classic, that's expected until it gets real testing -- same as
  every Forever bug fixed so far.
- Settings now has a "Manage WoW installs..." link that reopens the setup
  tool's WoW picker directly (skipping the already-done prerequisite/login
  steps), so you can add a flavor you skipped the first time without a
  fresh download.

## v1.2.0
- Conversations now persist across closing and reopening the app (including
  an update's own relaunch) -- previously every restart silently started a
  brand new conversation with no memory of anything asked before, which was
  never a deliberate choice, just something nobody had wired up yet. The
  visible history is restored too, with a marker showing where it picked
  back up. "⟲ New" still starts a genuinely fresh conversation and clears
  the saved history.

## v1.1.7
- Fixed equipped gear and bag items showing as empty even when clearly
  equipped/carried. The very first sync right after login/reload was
  running before WoW's own inventory cache had finished populating --
  confirmed directly in-game (a manual `/claudesync` later in the same
  session found the same gear just fine). The first post-login/reload sync
  now waits 2 seconds before collecting data; other refresh triggers
  (quest/zone changes, well into an already-loaded session) are unaffected.

## v1.1.6
- Fixed a real in-game Lua error introduced in v1.1.0 ("Claude Context sync
  failed: ...ClaudeContext.lua:175: attempt to call a nil value") that hit
  on every login/reload once a player had gear equipped. `GetItemInfo`
  turned out to be `nil` on WoW Forever, unlike the quest-log/bags APIs
  which were verified against a reference addon before shipping -- this one
  wasn't. Equipped-gear and bag-item names now try `C_Item.GetItemInfo`,
  then the classic global, then fall back to the raw item link, and never
  crash the sync regardless of which (if any) is available.

## v1.1.5
- The update download now genuinely resumes after a dropped connection
  (confirmed the CDN honors HTTP Range requests) instead of restarting from
  scratch each retry -- the percentage now only ever climbs, never resets,
  and the "attempt N/6, retrying..." messaging is gone (it's an
  implementation detail, not something worth surfacing).
- Installing an update no longer walks through the full multi-page setup
  wizard. It now quietly re-verifies the same things the wizard checks
  (Claude Desktop present, still logged in, WoW addon folder still valid),
  re-copies the files, and shows a brief green "Updated to vX" confirmation
  in the title bar instead. Only falls back to the full wizard if a check
  actually fails (e.g. logged out, Claude Desktop uninstalled) -- that
  genuinely needs the guided flow again.

## v1.1.4
- The ⚙ and ⓘ title bar icons now toggle -- click again to close, same as
  the Settings dialog's X or the tips dialog's "Got it" button.
- Added a tip explaining the "⟲ New" button (starts a fresh conversation).

## v1.1.3
- Added a "ⓘ" info button next to the gear icon in the title bar --
  reopens the first-run tips list anytime, not just automatically on first
  launch.

## v1.1.2
- Added a Stop button (the "Ask" button turns into "Stop" while a question
  is in flight) to cancel an accidental or unwanted query instead of having
  to wait it out.
- Fixed the update button showing a doubled "v" (e.g. "vv1.1.1") -- was
  always cosmetic, never affected which file got downloaded.
- The update download now shows real progress (percentage, or MB if the
  server doesn't report a size) instead of a static "Downloading..." with
  no feedback.
- Settings shows "Model: Sonnet" as plain info text (it's fixed, not a
  choice -- see v1.1.1) so it's still clear which model your effort level
  is trading off against.
- A one-time toast on startup confirms the app is running in the system
  tray, since Windows hides newly-added tray icons in the overflow area by
  default and the icon alone isn't a reliable sign it started.

## v1.1.1
- Removed the Opus/Fable model options from Settings -- Claude model is now
  fixed to Sonnet (existing installs that had Opus or Fable selected are
  reset to Sonnet automatically). You can still choose an effort level.

## v1.1.0
- Settings panel (⚙ gear icon): choose the Claude model (Sonnet/Opus/Fable)
  and effort level (Low/Medium/High), with Sonnet + Medium recommended by
  default. Also edit either hotkey from here, live.
- Window position and size are now remembered between sessions.
- The WoW addon now also reports equipped gear (all 17 slots, with item
  level), bag contents and gold, and a best-effort talent-points summary
  (WoW Forever's talent API isn't fully confirmed, so this degrades to
  "no talent data" gracefully rather than guessing).
- Crash recovery: unexpected errors are now logged to
  `%LOCALAPPDATA%\ClaudeWowOverlay\crash.log` instead of silently vanishing
  (a real risk for a windowed app with no console), and the box shows a
  short "something went wrong" note instead of freezing up.
- System tray icon: stays available when the box is hidden, with a
  right-click menu (Show/Hide, New conversation, Settings, Quit) and a
  toast notification when an answer finishes while the box is hidden.
- Right-click the camera button (or the screenshot area) to drag-select
  just part of the screen instead of always capturing the whole desktop --
  useful for cropping out just a comparison tooltip.
- Clearer errors when you're offline (checked up front, before waiting out
  a timeout) or when Claude is rate-limited/over quota.
- A proper "Apps & Features" uninstall entry -- Windows Settings > Apps can
  now remove the app, the WoW addon, the shortcut, and saved settings
  cleanly, instead of needing to delete folders by hand.
- Expanded first-run tips to cover all of the above.

## v1.0.3
- Shows the running version (e.g. `v1.0.3`) next to the title bar at all
  times. Turns into the green "Update to vX" button only when a newer
  version is actually available.
- A failed update download now resets back to a retryable button instead of
  getting stuck on "Downloading update...".

## v1.0.2
- Fixed the in-app updater: GitHub's release-asset CDN turned out to be
  genuinely flaky for a ~28MB file (three different real attempts each
  truncated at a different byte count before one came through complete).
  Now verifies the downloaded size against `Content-Length` and retries
  (up to 6x) until it's genuinely complete, instead of trusting a partial
  transfer.

## v1.0.1
- Test release to verify the update-check/download flow end-to-end. No
  other functional changes from v1.0.0. (This is the version that surfaced
  the CDN flakiness fixed in v1.0.2.)

## v1.0.0
First public release. Everything built in the initial session:
- Floating, draggable, resizable overlay box (`Ctrl+Shift+Space` to
  show/hide), styled to match a small dark/purple aesthetic.
- Runs on the local `claude.exe` CLI via the user's own OAuth login --
  nothing routes through anyone else.
- Companion `ClaudeContext` WoW addon: character, location, and quest log
  read from its SavedVariables file and fed into every question
  automatically, with explicit staleness/age awareness (only a
  login/reload/quest-change/new-zone refreshes it).
- Screenshot analysis (`Ctrl+Shift+S`, works even while the box is hidden
  so hovering a tooltip in-game doesn't get disturbed) via the CLI's Read
  tool.
- `[MAPLOC]` tag -> clickable "Copy location" button -> `/claudemark`
  addon command -> native in-game waypoint pin.
- `[SAVENOTE]` tag -> persistent per-character memory (stat priority,
  talent build, rotation, profession milestones), automatically reused on
  later questions instead of re-researching, with per-category
  level-staleness judgment.
- Ranked WoW Forever source hierarchy (official Blizzard > Wowhead > Icy
  Veins > boosting-service sites as backup only), with explicit fallback
  disclosure when nothing Forever-specific exists and general
  Vanilla/Classic knowledge is used instead.
- Standalone installer (`installer.py`) -- prerequisite checks, guided
  Claude login, WoW Forever auto-detection, Start Menu shortcut. Both it
  and the overlay compile to dependency-free exes via PyInstaller.
- Auto-update check on startup against GitHub Releases.
