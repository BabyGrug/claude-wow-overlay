# Claude WoW Overlay

A floating, always-on-top desktop app for asking Claude questions while
playing **World of Warcraft: Forever**. Streams live status + answers, knows
the player's character/location/quest log via a companion WoW addon, can
look at screenshots, marks map locations, remembers settled facts per
character, and checks for its own updates.

Repo: https://github.com/BabyGrug/claude-wow-overlay (public, hosted under
the `BabyGrug` org specifically so the owner's personal GitHub account never
appears anywhere -- see **Privacy** below).

## Architecture

- `overlay.py` -- the whole app (Tkinter GUI + subprocess calls to
  `claude.exe`). Compiled to a standalone exe via PyInstaller so end users
  need no Python installed.
- `installer.py` -- a Tkinter setup wizard that bundles the compiled
  overlay exe + WoW addon files as payload, walks a non-technical user
  through prerequisites, login, and installation. Also compiled standalone.
  Same exe, four modes selected by argv: no flag -> the full wizard (first
  install); `--uninstall` -> `UninstallApp` (this is what the registry's
  "Apps & Features" entry points at); `--update` -> `run_quiet_update()`,
  used by the overlay's own in-app updater for a routine version bump --
  re-checks prerequisites and re-copies files silently with no UI, only
  falling back to the full wizard if a check actually fails; `--manage` ->
  `InstallerApp(skip_welcome=True)`, launched from the overlay's own
  Settings ("Manage WoW installs...") -- skips straight to the same
  prerequisite-check page the normal flow uses (Claude's obviously already
  installed/logged in at that point) so a player can add a WoW flavor they
  skipped the first time without a fresh download.
- `payload/` -- what `installer.py` bundles: the compiled
  `ClaudeWowOverlay.exe` (gitignored, rebuilt every release), `icon.ico`,
  and `ClaudeContext/` (the WoW addon source, tracked in git).
- The live WoW addon (`Interface/AddOns/ClaudeContext/` inside the actual
  WoW install) is a **copy** of `payload/ClaudeContext/`. Edit one, copy the
  change to the other -- they're not symlinked. As of v1.2.x this may be
  copied into *multiple* WoW flavor installs on the same machine (Forever,
  Retail, Classic, Classic Era) -- see multi-flavor support below.

### Multi-flavor WoW support (v1.2.x+)

One addon file works across every WoW flavor -- `find_wow_installs()`
(`installer.py`) detects every `_*_` build folder under both Program Files
roots and labels each (Retail/Classic/Classic Era/WoW Forever), letting the
setup wizard's checklist page install into any/all of them at once.
`install_info.json` stores the chosen list (`addons_paths`, plural -- an
older singular `addons_path` from pre-1.2.1 installs is read transparently
for backward compat) so the uninstaller and the quiet updater both know
which install(s) to refresh without re-asking.

The addon itself detects which flavor it's running on via `GetGameFlavor()`
in `ClaudeContext.lua`: WoW Forever's interface/toc version (16000-19999,
checked via `select(4, GetBuildInfo())`) is checked *first*, since Forever
can share a folder name (`_classic_beta_`) with an unrelated beta and its
own `WOW_PROJECT_ID` is indistinguishable from Retail's (confirmed directly:
both report `1`) -- falling through to `WOW_PROJECT_ID`/the official
`WOW_PROJECT_*` constants only for everything else. This `gameVersion` field
is surfaced to Claude in both the per-turn context (`format_wow_context`)
and, read once at session-creation time, the system prompt's search-source
tier list (`build_system_prompt` picks Forever's hand-researched tier list
vs. a generic one) -- without it, Claude could confidently answer a Retail
question off stale Classic data with no way to notice the mismatch.

## The core loop

Every question shells out to the **local `claude.exe` CLI** (the same one
bundled with the Claude desktop app) via `-p`/`--print` headless mode, using
the user's own OAuth login -- this is *why* nothing ever routes through
anyone else. Model is fixed to Sonnet (Opus/Fable were removed from Settings
in v1.1.1 -- felt like it invited picking a pricier option by accident);
effort is user-configurable (Low/Medium/High, Medium recommended) via the
gear icon. `--restricted --allowedTools WebSearch,Read` (Read only added
when a screenshot's involved, plus `--add-dir` to the screenshots folder),
streamed via `--output-format stream-json --include-partial-messages`.

The conversation (`session_id`, passed as `--session-id`/`--resume`) and the
visible transcript persist across app restarts as of v1.2.0 -- `session_id`
lives in `settings.json`, the rendered text in
`%LOCALAPPDATA%\ClaudeWowOverlay\last_conversation.txt`, both cleared
together by "New". Before v1.2.0 every restart silently started a fresh
claude.exe session with no memory of anything asked before -- never a
deliberate choice, just something nobody had persisted.

Two hidden tags the model can emit at the end of an answer, stripped from
the visible text and turned into UI:
- `[MAPLOC zone="..." x=.. y=..]` -> a "Copy location" button
  (`/claudemark <zone> <x> <y>` for the addon's native-waypoint command)
- `[SAVENOTE category="..."]...[/SAVENOTE]` -> persists to
  `%LOCALAPPDATA%\ClaudeWowOverlay\character_notes\<realm>_<char>.json`,
  re-injected into every future turn for that character until it looks
  stale for its category (see `build_system_prompt`'s per-category
  staleness guidance).

## Hard-won gotchas -- do not relearn these the hard way again

1. **The Claude desktop app is MSIX-packaged.**
   `%APPDATA%\Roaming\Claude\claude-code\<ver>\claude.exe` is a *virtualized*
   path only visible from inside the app's own container -- a normal
   external process can't see it, even though `Get-Process` reports that
   exact path for the running process (it just echoes the target's own
   view, it doesn't resolve virtualization for the caller). The real,
   externally-reachable copy is at
   `%LOCALAPPDATA%\Packages\*Claude*\LocalCache\Roaming\Claude\claude-code\<ver>\claude.exe`.
   `find_claude_exe()` searches both, preferring the real one.

2. **`--append-system-prompt` only takes effect on the turn that CREATES a
   session (`--session-id`).** Every later `--resume` turn silently ignores
   a new value -- frozen at whatever was true on message 1 for that
   session's whole lifetime (confirmed directly with an APPLE/BANANA test).
   This is why WoW context, saved notes, and today's date live in the
   per-turn **prompt text** (`build_turn_context()`), not the system prompt
   -- only genuinely session-lifetime-stable instructions belong in
   `build_system_prompt()`.

3. **The `Read` tool is sandboxed to the process's working directory by
   default**, even under `--allowedTools Read`. Screenshots live under
   `%LOCALAPPDATA%\ClaudeWowOverlay\screenshots\`, outside that tree --
   needs `--add-dir <screenshots dir>` explicitly, or Read refuses with
   "outside allowed directory."

4. **WoW Forever's Lua only has `C_QuestLog`/`C_Map`, no legacy quest-log
   globals at all** (confirmed via QuestMaster's own compatibility layer --
   it hits "attempt to call a nil value" on the legacy globals on this
   client). Write addon code directly against the modern API; no
   legacy/modern branching needed like other Classic-line addons carry.

5. **A Lua pattern with an embedded literal `\0`** (e.g. `"[\0-\31]"`)
   **crashes WoW's sandboxed Lua** with "malformed pattern (missing ']')"
   on every call, even though vanilla Lua (verified with the `lupa` package)
   accepts the identical pattern fine. Fix: don't match control characters
   with a `[...]` range pattern at all -- scan byte-by-byte (`s:byte(i)`).
   Also avoid `%c` for the same purpose -- it's locale-dependent and matches
   bytes 128-191 (UTF-8 continuation bytes), which can corrupt non-ASCII
   text.

6. **`ReloadUI()` from addon code is a protected call that can fail
   SILENTLY** -- no error, nothing reloads -- even called synchronously and
   directly from a slash-command handler. Routing it through
   `C_Timer.After` reliably throws "Interface action failed because of an
   AddOn"; calling it direct can just silently no-op instead. The robust
   fix: don't call it from addon code at all, tell the player to type
   `/reload` themselves.

7. **`PLAYER_LOGIN` only fires on a genuine fresh login, never on
   `/reload`.** An addon that only listens for it never re-syncs on
   reload -- also register `PLAYER_ENTERING_WORLD`.

8. **Subprocess/`Start-Process` calls made through this session's own Bash
   or PowerShell tool don't automatically inherit registry-persisted env
   vars** (like `CLAUDE_CODE_OAUTH_TOKEN` set via `setx`). Always explicitly
   re-fetch (`(Get-ItemProperty -Path "HKCU:\Environment" -Name X).X`) and
   `$env:X = ...` in the *same* command before launching anything that
   needs it. Bitten by this more than once, including one where it silently
   broke a "live and ready" instance for several turns before being caught.

9. **Python's own file-copy (`shutil.copy2`), when that Python process is
   itself spawned via the Bash tool, can silently write somewhere real
   native Windows processes don't see** -- confirmed directly (Bash's `ls`
   showed a file that PowerShell's `Test-Path` and cmd's `dir` both called
   nonexistent, even with the PowerShell sandbox explicitly disabled). Root
   cause not fully pinned down. Practical fix: do real file deployments via
   `Copy-Item` in the PowerShell tool, never a Python one-liner run through
   Bash. Verify existence via PowerShell/cmd, not Bash, when it matters.

10. **GitHub's release-asset CDN (Azure Blob-backed) is genuinely flaky for
    a ~28MB file** -- confirmed directly: 3 separate download attempts each
    truncated at a *different* byte count before a 4th came through
    complete. A single `.read()` masks this as one opaque `IncompleteRead`;
    even chunked reads aren't enough alone, since EOF can arrive early
    *without* an exception (silently truncating the file). The real fix
    (`_download_and_install_update`): read in chunks AND verify the total
    against the `Content-Length` header, retrying (currently 6x) until
    genuinely byte-for-byte complete. **Also confirmed directly (curl with a
    `Range` header): this CDN honors real HTTP Range requests (`206 Partial
    Content`, `Accept-Ranges: bytes`)** -- so retries resume from wherever
    they died (`Range: bytes={total}-`, append mode) instead of restarting
    the whole file, with a local-file-size sanity check before trusting any
    resume (a mismatched partial file forces a clean restart rather than
    silently producing a corrupt result). No "attempt N/6" messaging is
    shown to the user -- just a percentage that only ever climbs.

11. **`SendKeys`-based testing risks hijacking whatever the user is
    actually typing** -- confirmed directly (a test `SendKeys.SendWait`
    landed inside the user's own in-progress message, since it goes to
    whatever has OS focus, not necessarily the target app). Don't use
    SendKeys for interactive testing; use process-alive checks, log files,
    or a direct backend call to the relevant Python function instead.

12. **A `--windowed` PyInstaller build has no console, so `sys.stdout`/
    `sys.stderr` are literally `None`.** Python's own default `sys.excepthook`
    and Tkinter's default `report_callback_exception` both try to write a
    traceback to `sys.stderr` -- on a windowed build that itself raises
    `AttributeError` and swallows the real error with zero trace. Fixed by
    redirecting both to `os.devnull` at import time if they're `None`, and by
    replacing all three exception-reporting paths (`sys.excepthook`,
    `threading.excepthook`, `Tk.report_callback_exception`) with a logger that
    only ever writes to `%LOCALAPPDATA%\ClaudeWowOverlay\crash.log`, never to
    stdout/stderr. See `_log_crash` / the crash-logging block near the top of
    `overlay.py`.

13. **`pystray`'s `Icon.run_detached()` spawns a non-daemon thread.** A
    Python process won't exit naturally while it's alive -- harmless in the
    real app since `quit()` calls `os._exit(0)` (a hard kill that doesn't wait
    for thread joins), but it means any throwaway test script that
    instantiates `ClaudeOverlay()` for real must also force-exit
    (`os._exit(...)`) rather than falling off the end of the script, or it'll
    hang forever waiting to join that thread.

14. **PyInstaller already ships a hook for `pystray`**
    (`hook-pystray.py` in `_pyinstaller_hooks_contrib`) -- it's picked up
    automatically, no `--hidden-import` needed for the tray icon to work in
    the compiled exe.

15. **A `tk.Toplevel` sized via a hardcoded `geometry()` call silently clips
    content pack() can't fit** -- same class of bug as the earlier pack()-vs-
    grid() layout bug (#2 in the original build), hit again in
    `_show_first_run_tips`: adding two more tips made the footer (and its
    "Got it" button) not fit in the guessed height, and pack() just never
    mapped it -- no error, no visible sign, the button was simply
    unclickable. Root-caused via `winfo_ismapped()` returning `0` on the
    button, not by guessing. Fixed by building all content first with the
    window withdrawn, then sizing from the real `winfo_reqheight()` before
    showing it -- never hardcode a Toplevel's height when its content can
    change.

16. **Can't fully delete a folder that contains your own currently-running
    exe in one step.** The uninstaller (`installer.py --uninstall`) needs to
    remove its own install directory, but it's a file inside that same
    directory while it's running. Fixed with the standard self-deleting-
    installer trick: remove everything else first (shortcut, addon folder,
    registry key), then hand off to a detached `cmd /c timeout /t 2 & rmdir
    /s /q <install_dir>` that outlives this process and finishes the job a
    couple seconds after it exits. No admin rights needed, since it's all
    under `%LOCALAPPDATA%`.

17. **`GetItemInfo` is `nil` on WoW Forever** -- shipped in v1.1.0 without
    verifying it first (unlike `C_QuestLog`/`C_Container`, which WERE
    checked against QuestMaster's source beforehand), and it caused a real
    in-game Lua error on every login/reload once a player had gear equipped
    ("...ClaudeContext.lua:175: attempt to call a nil value"). The line
    number pinpointed it exactly: `GetInventorySlotInfo` and
    `GetInventoryItemLink` both ran fine (a real item link came back), only
    the very next call -- `GetItemInfo(itemLink)` -- was calling a nil
    global. Fixed with `GetItemDisplayInfo()`: try `C_Item.GetItemInfo`
    first, then the classic global, then fall back to the raw item link if
    neither exists, wrapped in `pcall` so even a surprising error from
    whichever API *does* exist can't crash the sync. **Lesson**: the
    "foundational, predates-the-overhaul" reasoning that justified skipping
    verification for this one was wrong -- verify every WoW global against a
    reference addon before shipping, no matter how safe it seems, the same
    way `C_QuestLog`/`C_Container`/waypoints already were.

18. **The very first sync right after `PLAYER_LOGIN`/`PLAYER_ENTERING_WORLD`
    can see an empty inventory/equipment cache**, even though the exact same
    `GetInventorySlotInfo`/`GetInventoryItemLink`/`C_Container` calls work
    correctly moments later. Confirmed directly (not guessed): a `/run` of
    the raw API showed real equipped items, and a manual `/claudesync` later
    in the same session captured them fine -- only the automatic sync
    immediately on login/reload came back empty for `equipped`/`bags.items`
    (gold and quest data were unaffected, so this is specifically an
    inventory-cache race, not a broken sync in general). Fixed by wrapping
    the `PLAYER_LOGIN`/`PLAYER_ENTERING_WORLD` sync in `C_Timer.After(2,
    Sync)`; `QUEST_LOG_UPDATE`/`ZONE_CHANGED_NEW_AREA`/`PLAYER_LOGOUT` fire
    well into an already-loaded session so they stay immediate. General
    lesson: when something empirically works via `/run` but not through the
    addon's own event-triggered path, suspect timing before suspecting the
    API.

19. **`check_for_update()` used to be completely silent on failure**, which
    meant a genuine check failure (offline, GitHub down, rate-limited) was
    indistinguishable from "you're already up to date" -- confirmed directly
    that this is exactly what broke real update detection once: GitHub's
    public API is a shared 60-requests/hour-per-IP limit, and this project's
    own testing pattern (frequent app relaunches, each checking, plus direct
    `curl`/`gh` calls from the same machine) burned through it during a
    single active session (`curl -i https://api.github.com/repos/.../releases/latest`
    returned a plain `403 rate limit exceeded` with `X-RateLimit-Remaining: 0`
    -- not a guess, checked directly). Fixed: `check_for_update()` now
    returns `(latest, download_url, error)`, with `error` naming what went
    wrong. The quiet background/periodic check still ignores it on purpose
    (the user never asked for that one), but the manual "Check for updates
    now" (Settings) surfaces it. Also added periodic re-checking
    (`UPDATE_CHECK_INTERVAL_MS`, every 2h) -- before this, a check only ever
    ran once at startup, so an app left running for a while (which is the
    *normal* way to use it, especially with v1.2.0's conversation
    persistence removing the old reason to restart) would never learn a new
    version existed no matter how long it had been out.

## Testing patterns established on this project

- **Lua**: use the `lupa` package (a real Lua runtime, callable from Python)
  to actually *execute* addon code against a stubbed WoW API, rather than
  just reading the source by eye. Caught real bugs a pure review missed
  (the `\0` pattern crash, the `PLAYER_LOGIN`-not-`/reload` gap).
- **Python backend logic**: call `ClaudeOverlay._run_claude` (or whichever
  method) directly against a minimal `Stub` object exposing just the
  attributes it needs (`claude_exe`, `session_id`, `ui_queue`), rather than
  driving the full GUI. Faster, and caught real bugs (the `--add-dir` fix)
  without needing computer-use/screenshots.
- **Layout/UI**: instantiate `ClaudeOverlay()` for real, `.show()`,
  `.update_idletasks()`/`.update()`, then inspect `winfo_y()` /
  `winfo_height()` / `winfo_ismapped()` on the actual widgets. This is how
  the `pack()`-vs-`grid()` layout bug was caught and confirmed fixed.
- Always clean up scratch test scripts (`_*.py`) and kill test-launched
  processes before considering a change done.

## Release workflow

1. Bump `APP_VERSION` in `overlay.py`.
2. Rebuild `ClaudeWowOverlay.exe`: PyInstaller, `--onefile --windowed`,
   **absolute paths** for `--icon`/`--add-data` (relative paths break once
   `--specpath build` is also passed).
3. Copy the new exe into `payload/ClaudeWowOverlay.exe` via PowerShell
   `Copy-Item` (see gotcha #9 -- not a Python/Bash copy).
4. Rebuild `ClaudeWowOverlaySetup.exe` with the refreshed payload.
5. `git add -A && git commit` (repo-local identity is the generic
   `Claude WoW Overlay <noreply@users.noreply.github.com>`, never the real
   account -- see Privacy) `&& git tag vX.Y.Z && git push origin main vX.Y.Z`.
6. `gh release create vX.Y.Z dist/ClaudeWowOverlaySetup.exe --title vX.Y.Z --notes "..."`.
7. **Do NOT deploy the new exe to the dev machine's live install.** Leave
   `%LOCALAPPDATA%\ClaudeWowOverlay\ClaudeWowOverlay.exe` as it is after every
   release -- the user wants the still-running old version to detect the new
   GitHub release and go through its own real update-check/download/install
   flow instead, every time, as the standard way of verifying a release
   actually works end-to-end. (This reverses the original v1.0.x-era
   approach, which bypassed the updater to save a step -- that shortcut is
   no longer wanted.)
8. Update this file and `CHANGELOG.md` if anything gotcha-worthy or
   version-notable happened.

## Privacy

Hosted under the `BabyGrug` GitHub **organization**, not the owner's
personal account, specifically so the personal username never appears in
the repo URL. Commit authorship is set to a generic identity for the same
reason (`git config user.name/user.email`, repo-local only, not global).
Don't reintroduce the real username anywhere -- commit messages, release
notes, code comments, etc.

## Known limitations / open items

- **Grok/xAI integration**: asked about once, investigated lightly. The
  user has a "Grok Bot" desktop app, but it looks like a standard Electron
  chat client with no equivalent CLI+OAuth mechanism the way Claude Code
  has. Not built. If asked again: either confirm a genuine xAI CLI+OAuth
  equivalent exists, or it's a bigger fork into API-key-based auth (a real,
  different design decision, not a small addition).
- **Multi-flavor support (Retail/Classic/Classic Era) shipped in v1.2.x
  but is unverified beyond WoW Forever** -- there's no Retail or Classic
  client on this machine to test against, unlike Forever which has been
  verified repeatedly against a live client and QuestMaster's source. The
  legacy quest-log fallback (`CollectQuestsLegacy`), the `GetGameFlavor()`
  detection, and the TOC's non-Forever interface numbers are all built from
  general WoW API knowledge and defensive coding (try modern API, fall back
  to legacy, degrade to empty/nil rather than crash) but genuinely need the
  user's own real-world testing to confirm, the same way Forever's bugs
  (`GetItemInfo` nil, the login-sync timing race) only ever got found and
  fixed through live testing, not guessing. If the user reports Retail/
  Classic-specific issues, don't assume the API guess was right -- ask for
  a `/run` diagnostic the way gotcha #17/#18 did, don't just guess again.
- Versioning is semantic (vMAJOR.MINOR.PATCH) by default; revisit if the
  user asks for something simpler.
