r"""
Floating always-on-top Claude Q&A box, meant to sit on top of WoW (or anything
else) in Windowed / Borderless-Windowed mode.

Press the hotkey (default: Ctrl+Shift+Space) anywhere to show/hide the box.
Type a question, hit Enter, get an answer. Follow-up questions remember the
conversation until you click "New".

Requires WoW to run in Windowed or Borderless Windowed mode -- nothing can
draw on top of a true exclusive-fullscreen game.

One-time setup before this works:
    The desktop app is MSIX-packaged, so its bundled claude.exe actually
    lives under AppData\Local\Packages\...\LocalCache\, not the plain
    AppData\Roaming path Get-Process reports (that path only resolves from
    inside the app's own container). This script searches both locations.

    The desktop app's login also doesn't carry over to a freestanding
    claude.exe process. In your own terminal, run:
        claude setup-token
    and follow the browser prompt -- it prints a long-lived token. Save it
    permanently with (then open a NEW terminal / reboot the overlay):
        setx CLAUDE_CODE_OAUTH_TOKEN "paste-the-token-here"

Runs on Sonnet 5 at low effort, restricted to just the WebSearch tool (cuts
both cost and context bloat versus the full default toolset). Every question
is answered with live status ("Searching the web...", etc.) streamed into the
status line, and the answer itself streams into the box as it's generated --
see _run_claude() / TOOL_STATUS_LABELS.

If the companion "ClaudeContext" WoW addon is installed and the character has
done at least one /reload, this also reads that addon's SavedVariables file
(character, location, quest log) and feeds it to Claude as context on every
question -- see find_wow_context_file() / read_wow_context().
"""

import ctypes
import datetime
import glob
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.request
import uuid
from tkinter import font as tkfont

import keyboard
import pystray
from PIL import Image, ImageDraw, ImageGrab

# ============================================================================
# Crash logging -- installed before anything else touches sys.stdout/stderr.
# A --windowed PyInstaller build has NO console, so sys.stdout/sys.stderr are
# literally None -- Python's own default excepthook (and Tkinter's default
# report_callback_exception) both try to write a traceback to sys.stderr,
# which would itself raise AttributeError and swallow the real error with no
# trace at all. Everything here writes to a log file instead, never to
# stdout/stderr, so a crash is at least diagnosable after the fact.
# ============================================================================

CRASH_LOG_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", "."), "ClaudeWowOverlay", "crash.log"
)

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


def _log_crash(exc_type, exc_value, exc_tb, *, thread_name=None):
    try:
        os.makedirs(os.path.dirname(CRASH_LOG_PATH), exist_ok=True)
        header = f"=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        if thread_name:
            header += f" (thread: {thread_name})"
        header += f" -- v{APP_VERSION} ===\n"
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(header)
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write("\n")
    except Exception:
        pass  # logging the crash must never itself be what crashes the app


def _excepthook(exc_type, exc_value, exc_tb):
    _log_crash(exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    _log_crash(
        args.exc_type, args.exc_value, args.exc_traceback,
        thread_name=args.thread.name if args.thread else None,
    )


sys.excepthook = _excepthook
threading.excepthook = _thread_excepthook

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
GA_ROOT = 2


def force_foreground(hwnd: int):
    """Steal OS-level keyboard focus from whatever window (e.g. the game)
    currently holds it. Plain tkinter focus calls only affect focus *within*
    our own app -- they don't make Windows route real keystrokes to us.

    Windows silently refuses SetForegroundWindow from background processes
    (the "foreground lock" anti-annoyance feature) -- most overlay tools work
    around it by synthesizing a harmless Alt keypress right before asking,
    which resets the lock. Returns whether it actually worked.
    """
    hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    fg_hwnd = user32.GetForegroundWindow()
    if fg_hwnd == hwnd:
        user32.SetFocus(hwnd)
        return True

    fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
    cur_thread = kernel32.GetCurrentThreadId()
    attached = False
    try:
        user32.keybd_event(0x12, 0, 0, 0)       # ALT down
        user32.keybd_event(0x12, 0, 0x0002, 0)  # ALT up -- resets the foreground lock
        if fg_thread and fg_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        ok = bool(user32.SetForegroundWindow(hwnd))
        user32.BringWindowToTop(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, cur_thread, False)
    return ok and user32.GetForegroundWindow() == hwnd

WINDOW_W, WINDOW_H = 460, 360

APP_VERSION = "1.1.4"


def _icon_path():
    """icon.ico lives next to the exe once installed (the installer copies it
    there itself -- see installer.py's resource_path/install step), or next
    to this script when run straight from source for dev testing."""
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "icon.ico")
    return path if os.path.isfile(path) else None

# ============================================================================
# Settings -- persisted per-user, editable from the in-app Settings dialog.
# Everything here is a DEFAULT; the running values live in self.settings on
# the ClaudeOverlay instance, loaded from disk at startup and saved whenever
# the Settings dialog is confirmed, or the window moves/resizes.
# ============================================================================

SETTINGS_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", "."), "ClaudeWowOverlay", "settings.json"
)

DEFAULT_SETTINGS = {
    "model": "sonnet",
    "effort": "medium",
    "hotkey": "ctrl+shift+space",
    "screenshot_hotkey": "ctrl+shift+s",
    "window_x": None,   # None -> use the default top-right placement
    "window_y": None,
    "window_w": WINDOW_W,
    "window_h": WINDOW_H,
    "seen_first_run_tips": False,
}

# Model is fixed to Sonnet -- not user-selectable. (value, label) -- label is
# what the Settings dialog shows; value is what gets passed to claude.exe's
# --effort flag. Effort choices are a curated subset of the 5 the CLI
# supports (low/medium/high/xhigh/max) -- xhigh/max omitted from the UI as
# overkill for this app and a surprise-cost risk for casual users.
EFFORT_CHOICES = [
    ("low", "Low -- fastest, cheapest"),
    ("medium", "Medium -- balanced (recommended)"),
    ("high", "High -- most thorough, slower & pricier"),
]


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings.update(json.load(f))
        except Exception:
            pass
    # Model is no longer user-selectable -- always Sonnet, even overriding
    # whatever an older version of the Settings dialog may have saved for an
    # install that previously had Opus or Fable picked.
    settings["model"] = "sonnet"
    return settings


def save_settings(settings: dict):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


def format_hotkey_display(hotkey: str) -> str:
    """"ctrl+shift+s" -> "Ctrl+Shift+S" """
    return "+".join(part.capitalize() for part in hotkey.split("+"))
UPDATE_REPO = "BabyGrug/claude-wow-overlay"
UPDATE_CHECK_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_ASSET_NAME = "ClaudeWowOverlaySetup.exe"


def _version_tuple(v: str):
    parts = []
    for chunk in v.strip().lstrip("vV").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_for_update():
    """(latest_version, download_url) if a newer release exists, else
    (None, None). A plain anonymous metadata request -- no user data sent,
    nothing installed automatically. Silent on any failure (offline, GitHub
    down, rate-limited) since this is a nice-to-have, not core functionality."""
    try:
        req = urllib.request.Request(
            UPDATE_CHECK_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ClaudeWowOverlay"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        # GitHub tag names are "vX.Y.Z" -- strip the "v" here so every
        # downstream consumer (the "Update to vX" button text, etc.) gets a
        # bare version number and adds its own single "v" prefix, rather than
        # each caller having to remember whether this string already has one.
        latest = (data.get("tag_name") or "").strip().lstrip("vV")
        if not latest or _version_tuple(latest) <= _version_tuple(APP_VERSION):
            return None, None
        download_url = None
        for asset in data.get("assets", []):
            if asset.get("name") == UPDATE_ASSET_NAME:
                download_url = asset.get("browser_download_url")
                break
        if not download_url:
            return None, None
        return latest, download_url
    except Exception:
        return None, None

# Ranked, not a flat list -- researched 2026-09-20. Official > Wowhead > Icy
# Veins > everything else. The tier-4 sites are boosting-service content
# marketing (lfcarry, skycoach, mmogah, boostroom) plus a couple of smaller
# community aggregators -- accurate in spot-checks tonight, but no named-author
# accountability and a commercial incentive to oversell difficulty, so they're
# backup only, never a first stop. Maxroll.gg was checked and dropped -- no
# WoW Forever coverage yet, just retail/Midnight. games.gg was checked and
# dropped -- single uncredentialed author, no source citations.
WOW_SOURCE_TIER_1_OFFICIAL = [
    "worldofwarcraft.blizzard.com/en-us/forever",
    "us.forums.blizzard.com (WoW Forever category)",
]
WOW_SOURCE_TIER_2_PRIMARY = ["wowhead.com/forever"]
WOW_SOURCE_TIER_3_SECONDARY = ["icy-veins.com/wow-forever"]
WOW_SOURCE_TIER_4_BACKUP = [
    "lfcarry.com/guides", "skycoach.gg/blog/wow-forever",
    "mmogah.com/news/wow-forever", "mobalytics.gg/wow-forever",
    "classicwow.gg/forever", "classicwowforever.com", "wowforevertools.com",
    "boostroom.com/blog",
]

SCREENSHOTS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", "."), "ClaudeWowOverlay", "screenshots"
)
SCREENSHOT_MAX_WIDTH = 1600  # downscaled if wider -- plenty legible, cheaper/faster
SCREENSHOT_MAX_AGE_HOURS = 24


def _save_screenshot(img) -> str:
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOTS_DIR, f"shot_{int(time.time())}.png")
    img.save(path)
    return path


def _downscale_if_huge(img):
    if img.width > SCREENSHOT_MAX_WIDTH:
        new_h = int(img.height * (SCREENSHOT_MAX_WIDTH / img.width))
        return img.resize((SCREENSHOT_MAX_WIDTH, new_h), Image.LANCZOS)
    return img


def take_screenshot() -> str:
    """Grabs every monitor (not just the primary one -- WoW may not be on it),
    downscales if it's huge, and saves as a timestamped PNG. Returns the path.
    Caller is responsible for hiding the overlay window first so it isn't
    captured on top of the game."""
    img = _downscale_if_huge(ImageGrab.grab(all_screens=True))
    return _save_screenshot(img)


SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def _virtual_screen_bounds():
    """(x, y, w, h) of the full multi-monitor virtual desktop -- matches the
    coordinate space PIL.ImageGrab.grab(all_screens=True) uses, which can
    start at a negative x/y when a monitor sits left of/above the primary."""
    gsm = user32.GetSystemMetrics
    return (
        gsm(SM_XVIRTUALSCREEN), gsm(SM_YVIRTUALSCREEN),
        gsm(SM_CXVIRTUALSCREEN), gsm(SM_CYVIRTUALSCREEN),
    )


def cleanup_old_screenshots(max_age_hours=SCREENSHOT_MAX_AGE_HOURS):
    if not os.path.isdir(SCREENSHOTS_DIR):
        return
    cutoff = time.time() - max_age_hours * 3600
    for fname in os.listdir(SCREENSHOTS_DIR):
        fpath = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
        except OSError:
            pass

# Friendlier status-line text for tool_use blocks the model streams back.
# Anything not listed here falls back to f"Using {name}..." in _run_claude.
TOOL_STATUS_LABELS = {
    "ToolSearch": "Looking up search tool...",
    "WebSearch": "Searching the web...",
}

# Matches a trailing tag Claude emits when it has a specific in-game map
# location, e.g. [MAPLOC zone="Loch Modan" x=47.2 y=52.1] -- turned into a
# "Copy location" button that puts /claudemark <zone> <x> <y> on the
# clipboard, ready to paste into WoW chat. See _run_claude / _location_found.
LOCATION_TAG_RE = re.compile(
    r'\[MAPLOC\s+zone="([^"]*)"\s+x=([\d.]+)\s+y=([\d.]+)\]', re.IGNORECASE
)


def build_system_prompt() -> str:
    """Static behavior/tone instructions. --append-system-prompt only actually
    takes effect on the turn that CREATES a session (--session-id) -- on every
    --resume turn after that it's silently ignored by the CLI, so anything
    that can change turn-to-turn (the date, WoW context) must NOT live here or
    it'll freeze at whatever was true on message 1 for the rest of that
    session. Only put things here that are fine to fix for the session's
    whole lifetime. See build_turn_context() for the per-turn stuff."""
    return (
        "You are answering questions inside a small floating overlay box on top "
        "of a video game (World of Warcraft: Forever). Keep answers short and "
        "skimmable -- a few sentences or a short list -- unless explicitly asked "
        "for more detail. WoW Forever is a brand-new Classic+ game in active "
        "beta that postdates your training data -- search the web for anything "
        "about it (patches, quests, zones, classes, professions) rather than "
        "guessing. Never treat this as optional or skip straight to answering "
        "from memory for anything version/patch/beta-specific -- jump straight "
        "to a web search instead of asking clarifying questions first.\n\n"
        "Search in this strict priority order, and don't silently skip a tier "
        "just because an earlier one turned up something vague -- keep going "
        "until you have a real answer or have genuinely exhausted all four:\n"
        "1. Official (authoritative for dates/pricing/patch notes): "
        + ", ".join(WOW_SOURCE_TIER_1_OFFICIAL) + "\n"
        "2. Primary reference (most comprehensive, actively updated -- "
        "datamining, blue-post tracking): " + ", ".join(WOW_SOURCE_TIER_2_PRIMARY) + "\n"
        "3. Strong secondary (credentialed authors, actively maintained): "
        + ", ".join(WOW_SOURCE_TIER_3_SECONDARY) + "\n"
        "4. Backup only, if 1-3 genuinely have nothing on the topic: "
        + ", ".join(WOW_SOURCE_TIER_4_BACKUP) + " -- these are boosting-service "
        "content marketing, not dedicated reference sites. Treat them as a "
        "last resort, not a first stop, and weigh them accordingly.\n\n"
        "If you've genuinely checked tiers 1-4 and still found nothing "
        "Forever-specific on the topic, answer using how it worked in "
        "vanilla/Classic WoW instead (Forever is Vanilla-based, so that's "
        "usually a reasonable fallback) -- but say so explicitly and plainly, "
        "e.g. \"couldn't confirm this for Forever specifically -- this is "
        "based on how it worked in Classic, which may have changed.\" Never "
        "present a Classic-knowledge guess as confirmed Forever fact, and "
        "never quietly blend the two without flagging which is which.\n\n"
        "Each message you receive is prefixed with "
        "a fresh [context] block (today's date, and the player's live "
        "character/location/quest-log state when available) -- always trust "
        "that block over anything said earlier in the conversation, since it's "
        "re-read from disk on every single message and earlier ones may be stale. "
        "That block also tells you the context's age and whether it's missing "
        "entirely. It only gets refreshed by a login, a /reload, a quest-log "
        "change, or entering a new zone -- simply walking around triggers none "
        "of those, so treat the character/zone/quest fields as reliable but "
        "coordinates specifically as only a rough starting point once any real "
        "time has passed. If the player's own description conflicts with the "
        "context, the context looks more than a few minutes old for a "
        "coordinate-sensitive question, or it's missing entirely, say so "
        "plainly and suggest they type /reload in-game (or /claudesync then "
        "/reload if they haven't set up the addon) rather than silently "
        "trusting stale data or silently answering as if you have none.\n\n"
        "Whenever your answer includes a SPECIFIC, CONFIDENT in-game map "
        "location for something the player asked about (an NPC, object, "
        "resource node, etc. -- found via web search, the player's own quest "
        "objectives, or both), end your reply with exactly one tag in this "
        "exact format, on its own at the very end, nothing after it: "
        '[MAPLOC zone="<zone name>" x=<0-100> y=<0-100>] -- e.g. '
        '[MAPLOC zone="Loch Modan" x=47.2 y=52.1]. The app turns this into a '
        "clickable button that marks it on the player's map, so use the "
        "zone's exact proper name and coordinates on the normal 0-100 scale. "
        "Only include it when you're actually confident in specific "
        "coordinates -- never guess or approximate just to produce a tag, "
        "and omit it entirely for vague answers like \"somewhere in the "
        "zone\" or when multiple candidate locations exist and you're not "
        "sure which one is right.\n\n"
        "Sometimes a message starts with a bracketed note that a screenshot "
        "was just taken and saved to a path -- when you see that, use your "
        "Read tool on that exact path before doing anything else, since the "
        "player's real question is almost always about what's actually on "
        "their screen right now (e.g. comparing quest reward items, "
        "identifying an NPC, reading a tooltip). Describe what you see only "
        "as much as needed to justify your answer -- lead with the "
        "recommendation, don't narrate the whole screenshot.\n\n"
        "You also have a persistent per-character memory so the player never "
        "has to re-ask the same settled question. Whenever you give a "
        "confident, complete answer in one of these four categories, end "
        "your reply with a tag in this exact format (in addition to your "
        "normal answer, not instead of it), with the full answer text "
        "between the tags: [SAVENOTE category=\"<category>\"]<the answer, "
        "concise but complete enough to stand alone later>[/SAVENOTE]. Use "
        "more than one tag if you're confidently answering more than one "
        "category in the same reply. The four valid categories, and how "
        "level-sensitive each one is (i.e. how skeptical to be of an old "
        "saved note vs. the player's current level, shown in the [context] "
        "block):\n"
        "- stat_priority -- rarely changes with level for a given spec; "
        "trust an old saved note unless the player's build/spec has "
        "obviously changed.\n"
        "- talent_build -- changes at essentially every level as new points "
        "become available; treat a saved note as a starting point only and "
        "re-verify (then re-save) if the player has leveled up since it was "
        "captured.\n"
        "- rotation -- moderately level-sensitive; new abilities can reorder "
        "priority, so re-check if several levels have passed.\n"
        "- professions -- level-sensitive in the same way as talents; "
        "re-verify if their skill level has likely moved past the saved "
        "milestone.\n"
        "Never emit a tag for a vague, hedged, or incomplete answer -- only "
        "for something you'd be comfortable the player relying on later "
        "without re-asking."
    )


def build_turn_context() -> str:
    """Date + WoW addon context, freshly read on EVERY call (unlike the system
    prompt above) and prepended to the actual question text -- see _run_claude.
    This is the part that must never go stale mid-conversation."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    parts = [f"[context] Today's date is {today}."]

    context_path = find_wow_context_file()
    if not context_path:
        parts.append(
            "[No WoW character context file found anywhere. Either the "
            "ClaudeContext addon isn't installed/enabled in-game, or it's "
            "never been synced. If this matters for the question, tell the "
            "player rather than guessing at their character/location/quests.]"
        )
        return "\n".join(parts)

    try:
        data = read_wow_context(context_path)
        parts.append(format_wow_context(data))
        char = data.get("character", {})
        notes = load_character_notes(char.get("name", ""), char.get("realm", ""))
        notes_block = format_character_notes(notes, char.get("level"))
        if notes_block:
            parts.append(notes_block)
    except Exception as exc:
        parts.append(
            f"[A WoW context file exists at {context_path} but couldn't be "
            f"read ({exc}). Treat character/location/quest info as "
            "unavailable and mention that /reload in-game might fix it if "
            "it matters for the question.]"
        )
    return "\n".join(parts)


# ============================================================================
# WoW addon context (ClaudeContext addon -> SavedVariables -> here)
# ============================================================================

def find_wow_context_file():
    """Newest ClaudeContext.lua SavedVariables file across any WoW install/
    realm/character found under Program Files -- i.e. whichever character
    most recently logged out or /reloaded."""
    roots = []
    for env_var, default in (("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                              ("PROGRAMFILES", r"C:\Program Files")):
        roots.append(os.environ.get(env_var, default))
    seen = set()
    candidates = []
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        pattern = os.path.join(
            root, "World of Warcraft", "_*_", "WTF", "Account",
            "*", "*", "*", "SavedVariables", "ClaudeContext.lua",
        )
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


_LUA_STRING_RE = re.compile(r'ClaudeContextDB\s*=\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)


def _lua_unescape(s: str) -> str:
    """Undo the Lua source-level escaping SavedVariables applies on top of
    the addon's own JSON encoding (double-escaped: JSON string -> Lua string
    literal)."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nc = s[i + 1]
            if nc == "n":
                out.append("\n"); i += 2
            elif nc == "r":
                out.append("\r"); i += 2
            elif nc == "t":
                out.append("\t"); i += 2
            elif nc == '"':
                out.append('"'); i += 2
            elif nc == "\\":
                out.append("\\"); i += 2
            elif nc.isdigit():
                j = i + 1
                digits = ""
                while j < n and s[j].isdigit() and len(digits) < 3:
                    digits += s[j]
                    j += 1
                out.append(chr(int(digits)))
                i = j
            else:
                out.append(nc); i += 2
        else:
            out.append(c); i += 1
    return "".join(out)


def read_wow_context(path: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    match = _LUA_STRING_RE.search(content)
    if not match:
        raise ValueError("ClaudeContextDB not found in SavedVariables file")
    return json.loads(_lua_unescape(match.group(1)))


def _format_age(saved_at) -> str:
    """Turn the addon's savedAt timestamp into a plain-English age. Both
    sides read the same machine's local clock, so no timezone handling
    needed -- the addon's date() and this just need to agree, and they do."""
    if not saved_at:
        return "at an unknown time"
    try:
        saved_dt = datetime.datetime.strptime(saved_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "at an unknown time"

    age = (datetime.datetime.now() - saved_dt).total_seconds()
    if age < 0:
        return "just now"
    if age < 60:
        return "less than a minute ago"
    if age < 3600:
        minutes = int(age // 60)
        return f"about {minutes} minute{'s' if minutes != 1 else ''} ago"
    if age < 86400:
        hours = age / 3600
        return f"about {hours:.1f} hours ago"
    days = age / 86400
    return f"about {days:.1f} days ago"


def format_wow_context(data: dict) -> str:
    char = data.get("character", {})
    loc = data.get("location", {})
    quests = data.get("quests", [])

    lines = [
        "[Live WoW Forever character context -- use this to figure out who/"
        "where the player means (e.g. an NPC named in a quest objective) "
        "before searching, rather than asking them to clarify]",
        f"Character: {char.get('name', '?')} -- Level {char.get('level', '?')} "
        f"{char.get('race', '')} {char.get('class', '')} "
        f"({char.get('faction', '')}), realm {char.get('realm', '')}",
    ]

    loc_bits = [b for b in (loc.get("zone"), loc.get("subzone")) if b]
    coord_bit = f" at ({loc['x']}, {loc['y']})" if "x" in loc and "y" in loc else ""
    lines.append(f"Location: {' > '.join(loc_bits)}{coord_bit}")

    if quests:
        lines.append(f"Active quests ({len(quests)}):")
        for q in quests[:25]:
            level_bit = f" (lvl {q['level']})" if q.get("level") else ""
            objs = "; ".join(q.get("objectives", [])) or "no objective text"
            lines.append(f"- {q.get('title', '?')}{level_bit}: {objs}")
    else:
        lines.append("Active quests: none logged")

    equipped = data.get("equipped", [])
    if equipped:
        gear_bits = []
        for item in equipped:
            ilvl_bit = f" (ilvl {item['ilvl']})" if item.get("ilvl") else ""
            gear_bits.append(f"{item.get('slot', '?')}: {item.get('name', '?')}{ilvl_bit}")
        lines.append("Equipped gear: " + "; ".join(gear_bits))
    else:
        lines.append("Equipped gear: none reported")

    talents = data.get("talents")
    if talents:
        lines.append(
            f"Talent points spent (best-effort -- WoW Forever's talent API "
            f"isn't fully confirmed, treat as approximate): {talents}"
        )

    bags = data.get("bags")
    if bags:
        gold, silver, copper = bags.get("gold", 0), bags.get("silver", 0), bags.get("copper", 0)
        items = bags.get("items", [])
        items_bit = ", ".join(items[:20]) if items else "empty"
        more_bit = f" (+{len(items) - 20} more)" if len(items) > 20 else ""
        lines.append(
            f"Bags: {gold}g {silver}s {copper}c, carrying: {items_bit}{more_bit}"
        )

    lines.append(
        f"(captured {_format_age(data.get('savedAt'))}, at "
        f"{data.get('savedAt', 'an unknown time')} -- remember: only a "
        "login/reload/quest-log-change/new-zone refreshes this, so if real "
        "time has passed the player may well have moved since, especially "
        "the coordinates)"
    )
    return "\n".join(lines)


# ============================================================================
# Per-character saved notes -- Claude's own persistent memory, distinct from
# the live WoW addon context above. Populated automatically via a [SAVENOTE]
# tag the model emits (see build_system_prompt), so repeat questions like
# "what's my stat priority again" don't need a fresh web search every time.
# ============================================================================

NOTE_CATEGORIES = {
    "stat_priority": "Stat priority",
    "talent_build": "Talent build / next point",
    "rotation": "Rotation & ability unlocks",
    "professions": "Profession milestones",
}

NOTES_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", "."), "ClaudeWowOverlay", "character_notes"
)

SAVENOTE_RE = re.compile(
    r'\[SAVENOTE\s+category="([^"]*)"\]\s*(.*?)\s*\[/SAVENOTE\]',
    re.IGNORECASE | re.DOTALL,
)


def _character_note_path(char_name: str, realm: str) -> str:
    safe = re.sub(r'[^\w\- ]', "_", f"{realm}_{char_name}").strip() or "unknown"
    os.makedirs(NOTES_DIR, exist_ok=True)
    return os.path.join(NOTES_DIR, f"{safe}.json")


def load_character_notes(char_name: str, realm: str) -> dict:
    if not char_name:
        return {}
    path = _character_note_path(char_name, realm)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("notes", {})
    except Exception:
        return {}


def save_character_note(char_name: str, realm: str, category: str, value: str, level):
    if not char_name or category not in NOTE_CATEGORIES or not value.strip():
        return
    path = _character_note_path(char_name, realm)
    data = {"character": char_name, "realm": realm, "notes": {}}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data.setdefault("notes", {})
    data["notes"][category] = {
        "value": value.strip(),
        "level_captured": level,
        "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def format_character_notes(notes: dict, current_level) -> str:
    if not notes:
        return ""
    lines = [
        "[Saved notes for this character -- worked out and confirmed in an "
        "earlier session. Use these directly instead of re-searching unless "
        "they look stale for your current level (see the per-category "
        "guidance in your instructions) -- if you refresh one, re-emit its "
        "[SAVENOTE] tag with the updated answer so it replaces the old one.]"
    ]
    for category, entry in notes.items():
        label = NOTE_CATEGORIES.get(category, category)
        captured = entry.get("level_captured")
        level_bit = ""
        if isinstance(captured, int) and isinstance(current_level, int):
            level_bit = f" (saved at level {captured}, now level {current_level})"
        lines.append(f"- {label}{level_bit}: {entry.get('value', '')}")
    return "\n".join(lines)


def get_current_character_identity():
    """(name, realm, level) for whichever character's context file was most
    recently written, or (None, None, None) if none is available. Used to
    know which character's notes file a fresh [SAVENOTE] should write into."""
    context_path = find_wow_context_file()
    if not context_path:
        return None, None, None
    try:
        char = read_wow_context(context_path).get("character", {})
        return char.get("name"), char.get("realm"), char.get("level")
    except Exception:
        return None, None, None


def _internet_available(timeout: float = 3.0) -> bool:
    """A quick TCP handshake to the exact host claude.exe itself needs to
    reach -- not a generic ping target. Run this before spawning the CLI so
    a genuinely offline player gets an immediate, honest "you're offline"
    message instead of waiting out the full subprocess timeout and then
    having to guess what the CLI's own network-failure text looks like."""
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def find_claude_exe() -> str:
    """Locate the bundled Claude Code CLI, preferring the newest version.

    The desktop app is MSIX-packaged. A plain external process (this script)
    can't see the app's virtualized %APPDATA%\\Roaming\\Claude\\claude-code
    path -- that only resolves from inside the app's own container. The real,
    externally-reachable copy lives under the package's LocalCache instead.
    """
    localappdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    patterns = [
        os.path.join(localappdata, "Packages", "*Claude*", "LocalCache",
                     "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe"),
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    if not candidates:
        raise FileNotFoundError(
            "Could not find claude.exe under AppData\\Local\\Packages\\*Claude*\\"
            "LocalCache\\Roaming\\Claude\\claude-code\\. Is the Claude desktop "
            "app installed?"
        )

    def version_key(path: str):
        version = os.path.basename(os.path.dirname(path))
        parts = []
        for chunk in version.split("."):
            try:
                parts.append(int(chunk))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    candidates.sort(key=version_key, reverse=True)
    return candidates[0]


class ClaudeOverlay:
    def __init__(self):
        self.settings = load_settings()

        self.claude_exe = None
        self.claude_exe_error = None
        try:
            self.claude_exe = find_claude_exe()
        except FileNotFoundError as exc:
            self.claude_exe_error = str(exc)

        self.session_id = None
        self.busy = False
        self._streaming = False
        self._current_proc = None
        self._stopped_by_user = False
        self.ui_queue = queue.Queue()

        try:
            cleanup_old_screenshots()
        except Exception:
            pass  # never let housekeeping block startup

        self.root = tk.Tk()
        self.root.report_callback_exception = self._tk_callback_exception
        self.root.title("Claude")
        icon_path = _icon_path()
        if icon_path:
            try:
                self.root.iconbitmap(icon_path)
            except tk.TclError:
                pass
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.96)
        except tk.TclError:
            pass

        w = self.settings.get("window_w") or WINDOW_W
        h = self.settings.get("window_h") or WINDOW_H
        x = self.settings.get("window_x")
        y = self.settings.get("window_y")
        if x is None or y is None:
            screen_w = self.root.winfo_screenwidth()
            x = screen_w - w - 40
            y = 60
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self._build_ui()
        self.root.withdraw()  # start hidden; hotkey brings it up

        self.root.after(100, self._drain_ui_queue)
        self._register_hotkey()
        self._active_hotkey = self.settings["hotkey"]
        self._active_screenshot_hotkey = self.settings["screenshot_hotkey"]

        if self.claude_exe_error:
            self._append_answer(f"[setup problem] {self.claude_exe_error}")

        if not self.settings.get("seen_first_run_tips"):
            self.root.after(300, self._show_first_run_tips)

        threading.Thread(target=self._check_for_update_bg, daemon=True).start()

        self.tray_icon = None
        self._setup_tray_icon()

    def _tk_callback_exception(self, exc_type, exc_value, exc_tb):
        """Replaces Tkinter's default report_callback_exception, which prints
        to sys.stderr -- None in a --windowed build, so the default handler
        would itself throw and the original error would vanish with zero
        trace. Log it to disk and surface a short note in the status line
        instead of letting the box silently stop responding."""
        _log_crash(exc_type, exc_value, exc_tb, thread_name="tk-callback")
        try:
            self.status_var.set("⚠ Something went wrong -- logged, try again")
        except Exception:
            pass

    # ---------- UI ----------

    def _build_ui(self):
        bg = "#1e1e24"
        fg = "#e8e8ec"
        accent = "#7c5cff"

        outer = tk.Frame(self.root, bg=accent, bd=0)
        outer.pack(fill="both", expand=True)

        card = tk.Frame(outer, bg=bg, bd=0)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=0)  # titlebar -- fixed
        card.grid_rowconfigure(1, weight=1)  # answer area -- the only row that grows
        card.grid_rowconfigure(2, weight=0)  # status line -- fixed
        card.grid_rowconfigure(3, weight=0)  # input row -- fixed

        # --- draggable titlebar ---
        titlebar = tk.Frame(card, bg="#141419", height=32)
        titlebar.grid(row=0, column=0, sticky="ew")
        titlebar.grid_propagate(False)

        title_lbl = tk.Label(
            titlebar, text="Claude", bg="#141419", fg=fg,
            font=("Segoe UI", 10, "bold"), padx=10,
        )
        title_lbl.pack(side="left")

        quit_btn = tk.Label(
            titlebar, text="Quit", bg="#141419", fg="#9a9aa2",
            font=("Segoe UI", 8), padx=6, cursor="hand2",
        )
        quit_btn.pack(side="left")
        quit_btn.bind("<Button-1>", lambda e: self.quit())
        quit_btn.bind("<Enter>", lambda e: quit_btn.config(fg="#ff6b6b"))
        quit_btn.bind("<Leave>", lambda e: quit_btn.config(fg="#9a9aa2"))

        settings_btn = tk.Label(
            titlebar, text="⚙", bg="#141419", fg="#9a9aa2",
            font=("Segoe UI", 10), padx=6, cursor="hand2",
        )
        settings_btn.pack(side="left")
        settings_btn.bind("<Button-1>", lambda e: self._open_settings_dialog())
        settings_btn.bind("<Enter>", lambda e: settings_btn.config(fg=accent))
        settings_btn.bind("<Leave>", lambda e: settings_btn.config(fg="#9a9aa2"))

        # Reopens the same tips dialog shown automatically on first run --
        # otherwise there was no way back to it once dismissed.
        info_btn = tk.Label(
            titlebar, text="ⓘ", bg="#141419", fg="#9a9aa2",
            font=("Segoe UI", 10), padx=6, cursor="hand2",
        )
        info_btn.pack(side="left")
        info_btn.bind("<Button-1>", lambda e: self._show_first_run_tips())
        info_btn.bind("<Enter>", lambda e: info_btn.config(fg=accent))
        info_btn.bind("<Leave>", lambda e: info_btn.config(fg="#9a9aa2"))

        # The X just hides the box (same as the hotkey) -- it does NOT exit
        # the app, since the hotkey needs the process alive to bring it back.
        # Actually quitting is the "Quit" button above, next to the title.
        close_btn = tk.Label(
            titlebar, text="✕", bg="#141419", fg="#9a9aa2",
            font=("Segoe UI", 11), padx=10, cursor="hand2",
        )
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self.hide())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=accent))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg="#9a9aa2"))

        new_btn = tk.Label(
            titlebar, text="⟲ New", bg="#141419", fg="#9a9aa2",
            font=("Segoe UI", 9), padx=8, cursor="hand2",
        )
        new_btn.pack(side="right")
        new_btn.bind("<Button-1>", lambda e: self.new_conversation())
        new_btn.bind("<Enter>", lambda e: new_btn.config(fg=accent))
        new_btn.bind("<Leave>", lambda e: new_btn.config(fg="#9a9aa2"))

        # Shows the running version by default (dim, not clickable); becomes
        # a live "Update to vX" button once check_for_update() (kicked off in
        # __init__) actually finds something newer. See _show_update_button.
        self._update_available = False
        self.update_btn = tk.Label(
            titlebar, text=f"v{APP_VERSION}", bg="#141419", fg="#6f6f78",
            font=("Segoe UI", 9, "bold"), padx=8,
        )
        self.update_btn.pack(side="right")
        self.update_btn.bind(
            "<Enter>",
            lambda e: self.update_btn.config(fg="white") if self._update_available else None,
        )
        self.update_btn.bind(
            "<Leave>",
            lambda e: self.update_btn.config(fg="#4caf50") if self._update_available else None,
        )

        for widget in (titlebar, title_lbl):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._do_drag)

        # --- answer area (row 1: the only row allowed to grow) ---
        answer_frame = tk.Frame(card, bg=bg)
        answer_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(8, 4))
        answer_frame.grid_columnconfigure(0, weight=1)
        answer_frame.grid_rowconfigure(0, weight=1)

        self.answer_box = tk.Text(
            answer_frame, bg=bg, fg=fg, wrap="word", bd=0,
            font=("Segoe UI", 10), state="disabled",
            insertbackground=fg,
        )
        self.answer_box.grid(row=0, column=0, sticky="nsew")

        scrollbar = tk.Scrollbar(answer_frame, command=self.answer_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.answer_box.config(yscrollcommand=scrollbar.set)
        self.answer_box.tag_configure("status", foreground="#9a9aa2")
        self.answer_box.tag_configure("error", foreground="#ff6b6b")

        # --- status line (row 2: fixed height) ---
        self.status_var = tk.StringVar(value="Ready")
        status_lbl = tk.Label(
            card, textvariable=self.status_var, bg=bg, fg="#6f6f78",
            font=("Segoe UI", 8), anchor="w", padx=10,
        )
        status_lbl.grid(row=2, column=0, sticky="ew")

        # --- input row (row 3: fixed height) ---
        input_frame = tk.Frame(card, bg=bg)
        input_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 10))
        input_frame.grid_columnconfigure(0, weight=1)

        self.input_entry = tk.Entry(
            input_frame, bg="#2a2a33", fg=fg, insertbackground=fg,
            font=("Segoe UI", 10), bd=0,
        )
        self.input_entry.grid(row=0, column=0, sticky="ew", ipady=6, padx=(0, 6))
        self.input_entry.bind("<Return>", lambda e: self.send())
        self.input_entry.bind("<Button-1>", self._on_entry_click)

        # Stacked hotkey-label + camera icon, both clickable, so the shortcut
        # is always visible right on the button that does the same thing --
        # useful mainly as a reminder, since the whole point of the hotkey is
        # triggering this without touching the mouse (e.g. mid-tooltip-hover).
        screenshot_wrap = tk.Frame(input_frame, bg="#2a2a33", cursor="hand2")
        screenshot_wrap.grid(row=0, column=1, sticky="ns", padx=(0, 6))

        screenshot_hotkey_lbl = tk.Label(
            screenshot_wrap, text=format_hotkey_display(self.settings["screenshot_hotkey"]),
            bg="#2a2a33", fg="#6f6f78", font=("Segoe UI", 6),
        )
        screenshot_hotkey_lbl.pack(side="top", pady=(3, 0))
        self.screenshot_hotkey_lbl = screenshot_hotkey_lbl

        screenshot_btn = tk.Label(
            screenshot_wrap, text="\U0001F4F7", bg="#2a2a33",
            fg=fg, font=("Segoe UI", 10),
        )
        screenshot_btn.pack(side="top", padx=10, pady=(0, 3))

        for widget in (screenshot_wrap, screenshot_hotkey_lbl, screenshot_btn):
            widget.bind("<Button-1>", lambda e: self.ask_about_screen())
            # Right-click for a drag-to-select region instead of the whole
            # desktop -- left-click/hotkey stay full-screen on purpose, since
            # the whole point of those is capturing without touching the
            # mouse (e.g. mid-tooltip-hover); region-select is an explicit,
            # deliberate action so it's fine to require a drag for it.
            widget.bind("<Button-3>", lambda e: self.ask_about_screen_region())
            widget.bind("<Enter>", lambda e: (screenshot_btn.config(fg=accent),
                                               screenshot_hotkey_lbl.config(fg=accent)))
            widget.bind("<Leave>", lambda e: (screenshot_btn.config(fg=fg),
                                               screenshot_hotkey_lbl.config(fg="#6f6f78")))
        self.screenshot_btn = screenshot_btn

        send_btn = tk.Label(
            input_frame, text="Ask", bg=accent, fg="white",
            font=("Segoe UI", 9, "bold"), padx=12, cursor="hand2",
        )
        send_btn.grid(row=0, column=2, ipady=6)
        send_btn.bind("<Button-1>", lambda e: self._on_ask_or_stop())
        self.send_btn = send_btn

        # --- resize grip (bottom-right corner) ---
        # overrideredirect(True) strips ALL OS window chrome, resize handles
        # included -- this is the only way to resize without them back.
        resize_grip = tk.Label(
            card, text="⋰", bg=bg, fg="#4a4a55",
            font=("Segoe UI", 11), cursor="size_nw_se",
        )
        resize_grip.place(relx=1.0, rely=1.0, anchor="se", width=16, height=16)
        resize_grip.bind("<ButtonPress-1>", self._start_resize)
        resize_grip.bind("<B1-Motion>", self._do_resize)
        resize_grip.bind("<Enter>", lambda e: resize_grip.config(fg=accent))
        resize_grip.bind("<Leave>", lambda e: resize_grip.config(fg="#4a4a55"))

        self.root.minsize(300, 220)
        self.root.bind("<Escape>", lambda e: self.hide())

    def _on_entry_click(self, event):
        try:
            force_foreground(self.root.winfo_id())
        except Exception:
            pass
        self.input_entry.focus_force()
        self.status_var.set("Ready")

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event):
        x = self.root.winfo_pointerx() - self._drag_x
        y = self.root.winfo_pointery() - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _start_resize(self, event):
        self._resize_start_x = event.x_root
        self._resize_start_y = event.y_root
        self._resize_start_w = self.root.winfo_width()
        self._resize_start_h = self.root.winfo_height()

    def _do_resize(self, event):
        # minsize() isn't enforced here -- it only applies to interactive
        # resizes done through OS-drawn borders, which this window has none
        # of (overrideredirect strips them), so the floor is clamped by hand.
        dw = event.x_root - self._resize_start_x
        dh = event.y_root - self._resize_start_y
        new_w = max(300, self._resize_start_w + dw)
        new_h = max(220, self._resize_start_h + dh)
        self.root.geometry(f"{new_w}x{new_h}")

    # ---------- hotkey / show-hide ----------

    def _register_hotkey(self):
        try:
            keyboard.add_hotkey(self.settings["hotkey"], self._on_hotkey)
        except Exception as exc:
            self._append_answer(f"[hotkey setup failed] {exc}", tag="error")
        try:
            keyboard.add_hotkey(self.settings["screenshot_hotkey"], self._on_screenshot_hotkey)
        except Exception as exc:
            self._append_answer(f"[screenshot hotkey setup failed] {exc}", tag="error")

    def _reregister_hotkeys(self):
        """Called after the Settings dialog changes a hotkey -- tears down
        whatever's currently bound and re-registers from self.settings."""
        for hk in (self._active_hotkey, self._active_screenshot_hotkey):
            try:
                keyboard.remove_hotkey(hk)
            except Exception:
                pass
        self._register_hotkey()
        self._active_hotkey = self.settings["hotkey"]
        self._active_screenshot_hotkey = self.settings["screenshot_hotkey"]

    # ---------- settings dialog ----------

    def _open_settings_dialog(self):
        if getattr(self, "_settings_win", None) is not None:
            # Clicking the gear again while it's already open closes it --
            # same effect as the dialog's own X -- rather than just
            # re-focusing it, so the titlebar icon acts as a toggle.
            try:
                self._settings_win.destroy()
            except Exception:
                pass
            self._settings_win = None
            return

        bg, bg_dark, fg, accent = "#1e1e24", "#141419", "#e8e8ec", "#7c5cff"

        win = tk.Toplevel(self.root, bg=bg)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        # Stay hidden/unsized until content is built, then size to what it
        # actually needs (see the identical fix in _show_first_run_tips) --
        # a hardcoded guess here previously left slack space once the Model
        # section was removed, and would silently clip content the other
        # way if a section were ever added back.
        win.withdraw()
        mx, my = self.root.winfo_x(), self.root.winfo_y()
        w = 340
        self._settings_win = win

        outer = tk.Frame(win, bg=accent)
        outer.pack(fill="both", expand=True)
        card = tk.Frame(outer, bg=bg)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        titlebar = tk.Frame(card, bg=bg_dark, height=32)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        tk.Label(
            titlebar, text="Settings", bg=bg_dark, fg=fg,
            font=("Segoe UI", 10, "bold"), padx=10,
        ).pack(side="left")

        def do_close():
            self._settings_win = None
            win.destroy()

        close_lbl = tk.Label(
            titlebar, text="✕", bg=bg_dark, fg="#9a9aa2",
            font=("Segoe UI", 11), padx=10, cursor="hand2",
        )
        close_lbl.pack(side="right")
        close_lbl.bind("<Button-1>", lambda e: do_close())

        drag_state = {}

        def start_drag(e):
            drag_state["x"], drag_state["y"] = e.x, e.y

        def do_drag(e):
            win.geometry(f"+{win.winfo_pointerx() - drag_state.get('x', 0)}"
                         f"+{win.winfo_pointery() - drag_state.get('y', 0)}")

        titlebar.bind("<ButtonPress-1>", start_drag)
        titlebar.bind("<B1-Motion>", do_drag)

        content = tk.Frame(card, bg=bg)
        content.pack(fill="both", expand=True, padx=16, pady=12)

        effort_var = tk.StringVar(value=self.settings["effort"])

        # Not a selector (see v1.1.1 -- Opus/Fable removed) but still shown:
        # which model is in use matters for understanding token cost, same
        # reason the effort choices below spell out cheap/pricier.
        model_row = tk.Frame(content, bg=bg)
        model_row.pack(fill="x", pady=(4, 0))
        tk.Label(
            model_row, text="Model", bg=bg, fg=fg,
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).pack(side="left")
        tk.Label(
            model_row, text="Sonnet", bg=bg, fg="#9a9aa2",
            font=("Segoe UI", 9), anchor="e",
        ).pack(side="right")

        def build_choice_group(parent, title, choices, var):
            tk.Label(
                parent, text=title, bg=bg, fg=fg,
                font=("Segoe UI", 10, "bold"), anchor="w",
            ).pack(fill="x", pady=(8, 4))
            rows = {}

            def select(value):
                var.set(value)
                for v, (dot, lbl) in rows.items():
                    if v == value:
                        dot.config(text="◉", fg=accent)
                        lbl.config(fg=fg)
                    else:
                        dot.config(text="○", fg="#6f6f78")
                        lbl.config(fg="#9a9aa2")

            for value, label in choices:
                row = tk.Frame(parent, bg=bg, cursor="hand2")
                row.pack(fill="x", pady=1)
                dot = tk.Label(row, text="○", bg=bg, fg="#6f6f78", font=("Segoe UI", 10), padx=4)
                dot.pack(side="left")
                lbl = tk.Label(row, text=label, bg=bg, fg="#9a9aa2", font=("Segoe UI", 9), anchor="w")
                lbl.pack(side="left", fill="x", expand=True)
                rows[value] = (dot, lbl)
                for w in (row, dot, lbl):
                    w.bind("<Button-1>", lambda e, v=value: select(v))
            select(var.get())

        build_choice_group(content, "Effort", EFFORT_CHOICES, effort_var)

        tk.Label(
            content, text="Hotkeys", bg=bg, fg=fg,
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).pack(fill="x", pady=(14, 4))

        def hotkey_row(parent, label_text, current_value):
            row = tk.Frame(parent, bg=bg)
            row.pack(fill="x", pady=3)
            tk.Label(
                row, text=label_text, bg=bg, fg="#9a9aa2",
                font=("Segoe UI", 9), width=13, anchor="w",
            ).pack(side="left")
            entry = tk.Entry(row, bg="#2a2a33", fg=fg, insertbackground=fg, font=("Segoe UI", 9), bd=0)
            entry.insert(0, current_value)
            entry.pack(side="left", fill="x", expand=True, ipady=4)
            return entry

        hotkey_entry = hotkey_row(content, "Show/hide", self.settings["hotkey"])
        screenshot_entry = hotkey_row(content, "Screenshot", self.settings["screenshot_hotkey"])
        tk.Label(
            content, text="e.g. ctrl+shift+space -- restart may be needed if it doesn't take effect live",
            bg=bg, fg="#6f6f78", font=("Segoe UI", 7), anchor="w", wraplength=300, justify="left",
        ).pack(fill="x", pady=(2, 0))

        status_lbl = tk.Label(
            content, text="", bg=bg, fg="#ff6b6b", font=("Segoe UI", 8),
            anchor="w", wraplength=300, justify="left",
        )
        status_lbl.pack(fill="x", pady=(8, 0))

        def do_save():
            new_hotkey = hotkey_entry.get().strip().lower()
            new_screenshot_hotkey = screenshot_entry.get().strip().lower()
            if not new_hotkey or not new_screenshot_hotkey:
                status_lbl.config(text="Hotkeys can't be empty.")
                return
            if new_hotkey == new_screenshot_hotkey:
                status_lbl.config(text="The two hotkeys can't be the same.")
                return

            hotkeys_changed = (
                new_hotkey != self.settings["hotkey"]
                or new_screenshot_hotkey != self.settings["screenshot_hotkey"]
            )
            self.settings["effort"] = effort_var.get()
            self.settings["hotkey"] = new_hotkey
            self.settings["screenshot_hotkey"] = new_screenshot_hotkey

            if hotkeys_changed:
                try:
                    self._reregister_hotkeys()
                    self.screenshot_hotkey_lbl.config(text=format_hotkey_display(new_screenshot_hotkey))
                except Exception as exc:
                    status_lbl.config(text=f"Hotkey registration failed: {exc}")
                    return

            save_settings(self.settings)
            do_close()

        footer = tk.Frame(content, bg=bg)
        footer.pack(fill="x", pady=(14, 0), side="bottom")
        save_btn = tk.Label(
            footer, text="Save", bg=accent, fg="white",
            font=("Segoe UI", 9, "bold"), padx=14, pady=6, cursor="hand2",
        )
        save_btn.pack(side="right")
        save_btn.bind("<Button-1>", lambda e: do_save())

        win.update_idletasks()
        h = outer.winfo_reqheight()
        win.geometry(f"{w}x{h}+{mx + 40}+{my + 20}")
        win.deiconify()

    # ---------- first-run tips ----------

    def _show_first_run_tips(self):
        if getattr(self, "_tips_win", None) is not None:
            # Clicking the info icon again while it's already open closes it
            # -- same effect as "Got it" -- rather than just re-focusing it,
            # so the titlebar icon acts as a toggle.
            try:
                self._tips_win.destroy()
            except Exception:
                pass
            self._tips_win = None
            self.settings["seen_first_run_tips"] = True
            save_settings(self.settings)
            return

        bg, bg_dark, fg, accent = "#1e1e24", "#141419", "#e8e8ec", "#7c5cff"

        win = tk.Toplevel(self.root, bg=bg)
        self._tips_win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        # Stay hidden and unsized while content is built -- a hardcoded
        # height here previously left the footer/Got-it-button silently
        # unmapped by pack() whenever the tip list got taller than the
        # guessed number (the window's *explicit* geometry() size wins over
        # pack()'s natural sizing, so anything that doesn't fit just isn't
        # drawn, no error). Sized for real below, from actual content.
        win.withdraw()
        w = 380

        outer = tk.Frame(win, bg=accent)
        outer.pack(fill="both", expand=True)
        card = tk.Frame(outer, bg=bg)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        titlebar = tk.Frame(card, bg=bg_dark, height=32)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        tk.Label(
            titlebar, text="Welcome to Claude WoW Overlay", bg=bg_dark, fg=fg,
            font=("Segoe UI", 10, "bold"), padx=10,
        ).pack(side="left")

        content = tk.Frame(card, bg=bg)
        content.pack(fill="both", expand=True, padx=18, pady=14)

        tips = [
            (format_hotkey_display(self.settings["hotkey"]),
             "Show or hide the box, from anywhere -- even with WoW focused."),
            (format_hotkey_display(self.settings["screenshot_hotkey"]),
             "Ask about what's on your screen right now (quest reward "
             "choices, a tooltip you're hovering, anything visual)."),
            ("Right-click \U0001F4F7", "Drag-select just part of the screen "
             "instead of sending the whole desktop -- good for cropping out "
             "a comparison tooltip."),
            ("\U0001F4CD button", "Appears when Claude gives you a specific map "
             "location -- click it, then paste in WoW chat to drop a waypoint."),
            ("⚙ gear icon", "Change the model, effort level, or either "
             "hotkey, next to \"Claude\" in the title bar. Click it again "
             "to close Settings, same as its X."),
            ("⟲ New", "Starts a fresh conversation -- Claude forgets "
             "everything asked so far in this session."),
            ("Tray icon", "Claude keeps running in the system tray when "
             "hidden -- right-click it for Show/Hide, New, Settings, or Quit."),
            ("✕ vs Quit", "✕ just hides the box (same as the hotkey). "
             "\"Quit\" next to the title actually exits."),
            ("ⓘ button", "Come back to this list anytime -- it's next "
             "to the gear icon in the title bar. Click it again to close, "
             "same as \"Got it\" below."),
        ]
        for label, desc in tips:
            row = tk.Frame(content, bg=bg)
            row.pack(fill="x", pady=5)
            tk.Label(
                row, text=label, bg=bg, fg=accent, font=("Segoe UI", 9, "bold"),
                anchor="w", width=14, wraplength=110, justify="left",
            ).pack(side="left", anchor="n")
            tk.Label(
                row, text=desc, bg=bg, fg="#c8c8ce", font=("Segoe UI", 9),
                anchor="w", wraplength=210, justify="left",
            ).pack(side="left", fill="x", expand=True)

        def dismiss():
            self.settings["seen_first_run_tips"] = True
            save_settings(self.settings)
            self._tips_win = None
            win.destroy()

        footer = tk.Frame(content, bg=bg)
        footer.pack(fill="x", side="bottom", pady=(10, 0))
        got_it_btn = tk.Label(
            footer, text="Got it", bg=accent, fg="white",
            font=("Segoe UI", 9, "bold"), padx=16, pady=6, cursor="hand2",
        )
        got_it_btn.pack(side="right")
        got_it_btn.bind("<Button-1>", lambda e: dismiss())

        # Size the window to what the content actually needs, now that all
        # of it exists -- avoids ever again silently clipping the footer off
        # a fixed-height guess when the tip list changes.
        win.update_idletasks()
        h = min(outer.winfo_reqheight(), win.winfo_screenheight() - 80)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        win.deiconify()

        self.show()

    def _on_hotkey(self):
        # fires on keyboard library's own thread -- hop back to the Tk thread
        self.ui_queue.put(("toggle", None))

    def _on_screenshot_hotkey(self):
        # Works whether the box is currently shown or hidden -- the point is
        # to fire this WITHOUT touching the mouse, so a tooltip the player is
        # hovering over in-game doesn't get dismissed by moving to click.
        self.ui_queue.put(("screenshot_hotkey", None))

    # ---------- updates ----------

    def _check_for_update_bg(self):
        latest, download_url = check_for_update()
        if latest:
            self.ui_queue.put(("update_available", (latest, download_url)))

    def _show_update_button(self, payload):
        latest, download_url = payload
        self._update_available = True
        self.update_btn.config(text=f"⬆ Update to v{latest}", fg="#4caf50", cursor="hand2")
        self.update_btn.bind("<Button-1>", lambda e: self._start_update(latest, download_url))

    def _start_update(self, latest, download_url):
        if self.busy:
            return
        self._update_available = False
        self.update_btn.unbind("<Button-1>")
        self.update_btn.config(text="Downloading update... 0%", cursor="", fg="#4caf50")
        threading.Thread(
            target=self._download_and_install_update, args=(latest, download_url), daemon=True,
        ).start()

    def _show_update_progress(self, payload):
        total, expected = payload
        if expected:
            pct = min(100, int(total * 100 / expected))
            text = f"Downloading update... {pct}%"
        else:
            # Content-Length wasn't sent for some reason -- show raw progress
            # instead of a percentage of an unknown total.
            text = f"Downloading update... {total / (1024 * 1024):.1f} MB"
        self.update_btn.config(text=text)

    def _download_and_install_update(self, latest, download_url):
        installer_path = os.path.join(os.environ.get("TEMP", "."), UPDATE_ASSET_NAME)
        max_attempts = 6
        last_error = None
        # The GitHub-release CDN this redirects to has turned out to be
        # genuinely flaky for a ~28MB file -- confirmed by testing directly:
        # 3 different attempts each truncated at a DIFFERENT byte count
        # (4.1MB, 1.7MB, 25.5MB) before a 4th finally came through complete.
        # A single .read() masks this as one opaque IncompleteRead; reading
        # in chunks and explicitly checking the total against Content-Length
        # is what actually catches every truncation (a plain "empty chunk"
        # EOF check alone would silently accept a truncated file as done,
        # which is worse than an error -- also confirmed the hard way).
        for attempt in range(max_attempts):
            try:
                req = urllib.request.Request(download_url, headers={"User-Agent": "ClaudeWowOverlay"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    expected = resp.getheader("Content-Length")
                    expected = int(expected) if expected else None
                    total = 0
                    last_progress_ts = 0.0
                    with open(installer_path, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            total += len(chunk)
                            # Time-throttled, not per-chunk -- a ~28MB file is
                            # hundreds of 64KB chunks, too many UI events to
                            # push (and redraw) for every single one.
                            now = time.monotonic()
                            if now - last_progress_ts >= 0.15:
                                last_progress_ts = now
                                self.ui_queue.put(("update_progress", (total, expected)))
                    self.ui_queue.put(("update_progress", (total, expected)))
                    if expected is not None and total != expected:
                        raise IOError(f"incomplete download: got {total} of {expected} bytes")
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                self.ui_queue.put((
                    "status",
                    f"Download attempt {attempt + 1}/{max_attempts} incomplete, retrying...",
                ))
                time.sleep(2)

        if last_error:
            self.ui_queue.put((
                "answer",
                (f"Claude: [update failed after {max_attempts} attempts] {last_error}", "error"),
            ))
            # Restore the clickable "Update to vX" state (rather than leaving
            # it stuck on "Downloading update...") so retrying doesn't need a
            # full app restart.
            self.ui_queue.put(("update_available", (latest, download_url)))
            self.ui_queue.put(("done", None))
            return
        try:
            subprocess.Popen([installer_path])
        except Exception as exc:
            self.ui_queue.put(("answer", (f"Claude: [update downloaded but failed to launch] {exc}", "error")))
            self.ui_queue.put(("update_available", (latest, download_url)))
            self.ui_queue.put(("done", None))
            return
        self.ui_queue.put(("quit", None))

    def _drain_ui_queue(self):
        try:
            while True:
                action, payload = self.ui_queue.get_nowait()
                if action == "toggle":
                    self.toggle()
                elif action == "screenshot_hotkey":
                    self.ask_about_screen()
                elif action == "update_available":
                    self._show_update_button(payload)
                elif action == "update_progress":
                    self._show_update_progress(payload)
                elif action == "quit":
                    self.quit()
                elif action == "answer":
                    self._set_answer(payload)
                elif action == "answer_begin":
                    self._begin_streaming_answer()
                elif action == "answer_chunk":
                    self._append_stream_chunk(payload)
                elif action == "status":
                    self.status_var.set(payload)
                elif action == "location_found":
                    self._show_location_button(payload)
                elif action == "notes_saved":
                    self._show_notes_saved(payload)
                elif action == "done":
                    self._end_streaming_answer()
                    self._set_busy(False)
                    self._notify_if_hidden()
                elif action == "tray_toggle":
                    self.toggle()
                elif action == "tray_new":
                    self.new_conversation()
                    self.show()
                elif action == "tray_settings":
                    self.show()
                    self._open_settings_dialog()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def toggle(self):
        if self.root.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        try:
            ok = force_foreground(self.root.winfo_id())
        except Exception as exc:
            ok = False
            self._append_answer(f"[focus steal errored] {exc}", tag="error")
        self.root.focus_force()
        self.input_entry.focus_force()
        self.input_entry.icursor("end")
        if not ok:
            self.status_var.set("Click in the box to type -- auto-focus was blocked")
        else:
            self.status_var.set("Ready")

    def hide(self):
        self.root.withdraw()

    def quit(self):
        try:
            self.settings["window_x"] = self.root.winfo_x()
            self.settings["window_y"] = self.root.winfo_y()
            self.settings["window_w"] = self.root.winfo_width()
            self.settings["window_h"] = self.root.winfo_height()
            save_settings(self.settings)
        except Exception:
            pass
        for hk in (self._active_hotkey, self._active_screenshot_hotkey):
            try:
                keyboard.remove_hotkey(hk)
            except Exception:
                pass
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()
        os._exit(0)

    # ---------- system tray ----------

    def _fallback_tray_image(self):
        """Only used if icon.ico is somehow missing next to the exe -- a
        plain accent-colored dot beats pystray refusing to start at all."""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((4, 4, 60, 60), fill=(124, 92, 255, 255))
        return img

    def _setup_tray_icon(self):
        icon_path = _icon_path()
        try:
            image = Image.open(icon_path) if icon_path else self._fallback_tray_image()
        except Exception:
            image = self._fallback_tray_image()

        # Callbacks below run on pystray's own background thread, never the
        # Tk main thread -- Tkinter widgets aren't thread-safe, so (same
        # pattern as the global hotkey handlers) every one just pushes an
        # action onto ui_queue and lets _drain_ui_queue apply it on the main
        # thread instead of touching self.root directly.
        menu = pystray.Menu(
            pystray.MenuItem(
                "Show / Hide", lambda: self.ui_queue.put(("tray_toggle", None)),
                default=True,
            ),
            pystray.MenuItem("New conversation", lambda: self.ui_queue.put(("tray_new", None))),
            pystray.MenuItem("Settings...", lambda: self.ui_queue.put(("tray_settings", None))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.ui_queue.put(("quit", None))),
        )
        self.tray_icon = pystray.Icon("ClaudeWowOverlay", image, "Claude WoW Overlay", menu)
        try:
            self.tray_icon.run_detached()
            # Windows hides newly-added tray icons in the overflow ("^")
            # area by default, same as any other app -- so the icon alone
            # isn't a reliable "yes, it's running" signal on first launch.
            # A toast is a second, separate confirmation that doesn't depend
            # on the icon being visible. Delayed briefly since run_detached()
            # returns as soon as its thread *starts*, not once the native
            # tray window actually exists yet.
            self.root.after(1500, self._notify_tray_ready)
        except Exception as exc:
            self._append_answer(f"[tray icon failed to start] {exc}", tag="error")
            self.tray_icon = None

    def _notify_tray_ready(self):
        try:
            self.tray_icon.notify(
                "Running in the background. If you don't see the icon, "
                "check the ˄ arrow near the clock.",
                "Claude WoW Overlay is ready",
            )
        except Exception:
            pass

    def _notify_if_hidden(self):
        """Best-effort badge that an answer is ready -- only worth a toast if
        the player can't already see it (i.e. the box is currently hidden)."""
        if self.tray_icon is None:
            return
        try:
            if self.root.state() == "withdrawn":
                self.tray_icon.notify("Claude has an answer ready.", "Claude WoW Overlay")
        except Exception:
            pass

    # ---------- conversation ----------

    def new_conversation(self):
        self.session_id = None
        self.answer_box.config(state="normal")
        self.answer_box.delete("1.0", "end")
        self.answer_box.config(state="disabled")
        self.status_var.set("New conversation")

    def _append_answer(self, text, tag=None):
        self.answer_box.config(state="normal")
        if self.answer_box.get("1.0", "end").strip():
            self.answer_box.insert("end", "\n\n")
        self.answer_box.insert("end", text, tag)
        self.answer_box.see("end")
        self.answer_box.config(state="disabled")

    def _set_answer(self, payload):
        text, tag = payload
        self._append_answer(text, tag)

    def _begin_streaming_answer(self):
        self.answer_box.config(state="normal")
        if self.answer_box.get("1.0", "end").strip():
            self.answer_box.insert("end", "\n\n")
        self.answer_box.insert("end", "Claude: ")
        self.answer_box.see("end")
        self._streaming = True

    def _append_stream_chunk(self, text):
        if not self._streaming:
            self._begin_streaming_answer()
        self.answer_box.insert("end", text)
        self.answer_box.see("end")

    def _end_streaming_answer(self):
        if self._streaming:
            self.answer_box.config(state="disabled")
            self._streaming = False

    def _show_location_button(self, payload):
        """Replaces a [MAPLOC ...] tag (already visible from streaming -- it
        streamed in like any other text before we knew it was a tag) with a
        clickable button that copies /claudemark <zone> <x> <y> to the
        clipboard, ready to paste into WoW chat."""
        raw_tag = payload["raw_tag"]
        zone = payload["zone"]
        x = payload["x"]
        y = payload["y"]

        self.answer_box.config(state="normal")
        start_idx = self.answer_box.search(raw_tag, "1.0", "end")
        if start_idx:
            self.answer_box.delete(start_idx, f"{start_idx}+{len(raw_tag)}c")
        else:
            # Formatting drifted enough that the literal text didn't match --
            # still show the button, just appended rather than in-place.
            self.answer_box.insert("end", "\n")

        zone_label = zone if zone else "your current zone"
        btn = tk.Label(
            self.answer_box, text=f"\U0001F4CD Copy location: {zone_label} ({x}, {y})",
            bg="#7c5cff", fg="white", font=("Segoe UI", 9, "bold"),
            padx=8, pady=3, cursor="hand2",
        )

        def do_copy(event=None):
            cmd = f"/claudemark {zone} {x} {y}".strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(cmd)
            self.root.update()  # makes sure the clipboard content sticks on Windows
            btn.config(text="Copied! Paste into WoW chat", bg="#4caf50")

        btn.bind("<Button-1>", do_copy)
        self.answer_box.window_create("end", window=btn)
        self.answer_box.insert("end", "\n")
        self.answer_box.see("end")
        self.answer_box.config(state="disabled")

    def _show_notes_saved(self, payload):
        """Strips each [SAVENOTE ...]...[/SAVENOTE] block (already visible
        from streaming) and replaces the whole set with one small dim
        confirmation line, so the raw tag markup never stays on screen."""
        self.answer_box.config(state="normal")
        for raw_tag in payload["raw_tags"]:
            start_idx = self.answer_box.search(raw_tag, "1.0", "end")
            if start_idx:
                self.answer_box.delete(start_idx, f"{start_idx}+{len(raw_tag)}c")

        labels = ", ".join(payload["labels"])
        self.answer_box.insert(
            "end", f"\n\U0001F4BE Remembered: {labels}", "status",
        )
        self.answer_box.insert("end", "\n")
        self.answer_box.see("end")
        self.answer_box.config(state="disabled")

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.input_entry.config(state=state)
        if busy:
            self.send_btn.config(text="Stop", bg="#ff6b6b")
        else:
            self.send_btn.config(text="Ask", bg="#7c5cff")
            self._current_proc = None
            self.status_var.set("Ready")

    def _on_ask_or_stop(self):
        if self.busy:
            self.stop_query()
        else:
            self.send()

    def stop_query(self):
        """Kills the in-flight claude.exe call, if any -- lets an accidental
        or unwanted question be aborted instead of having to wait it out."""
        if not self.busy:
            return
        self._stopped_by_user = True
        if self._current_proc is not None:
            try:
                self._current_proc.terminate()
            except Exception:
                pass
        self.status_var.set("Stopping...")

    def send(self):
        if self.busy:
            return
        question = self.input_entry.get().strip()
        if not question:
            return
        self.input_entry.delete(0, "end")
        self._start_query(question)

    def ask_about_screen(self):
        """Hides the box, grabs every monitor, brings the box back, then asks
        Claude to look at what it just captured. Hiding first matters -- the
        box floats on top of the game, so without this it would screenshot
        itself sitting on top of whatever the player actually wants looked at."""
        if self.busy:
            return
        question = self.input_entry.get().strip() or (
            "Look at what's on my screen right now and help me decide what to do."
        )
        self.input_entry.delete(0, "end")

        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)  # let the window manager actually finish hiding first
        try:
            screenshot_path = take_screenshot()
            error = None
        except Exception as exc:
            screenshot_path = None
            error = str(exc)
        self.show()

        if error:
            self._append_answer(f"Claude: [error] couldn't take a screenshot: {error}", tag="error")
            return

        self._start_query(question, screenshot_path=screenshot_path)

    def ask_about_screen_region(self):
        """Same idea as ask_about_screen(), but lets the player drag-select
        just part of the screen first -- e.g. to crop out just the item
        comparison tooltip instead of sending the whole cluttered desktop."""
        if self.busy:
            return
        question = self.input_entry.get().strip() or (
            "Look at what's on my screen right now and help me decide what to do."
        )
        self.input_entry.delete(0, "end")

        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)

        try:
            full_img = ImageGrab.grab(all_screens=True)
        except Exception as exc:
            self.show()
            self._append_answer(f"Claude: [error] couldn't take a screenshot: {exc}", tag="error")
            return

        box = self._select_region()
        if box is None:
            self.show()
            return

        vx, vy, _, _ = _virtual_screen_bounds()
        x0, y0, x1, y1 = box
        try:
            region_img = _downscale_if_huge(full_img.crop((x0 - vx, y0 - vy, x1 - vx, y1 - vy)))
            screenshot_path = _save_screenshot(region_img)
        except Exception as exc:
            self.show()
            self._append_answer(f"Claude: [error] couldn't take a screenshot: {exc}", tag="error")
            return

        self.show()
        self._start_query(question, screenshot_path=screenshot_path)

    def _select_region(self):
        """A fullscreen, semi-transparent drag-to-select overlay. Blocks
        (via wait_window) until the player finishes a drag or cancels with
        Escape. Returns (x0, y0, x1, y1) in absolute screen coordinates, or
        None if cancelled / the drag was too small to be intentional."""
        vx, vy, vw, vh = _virtual_screen_bounds()

        sel = tk.Toplevel(self.root)
        sel.overrideredirect(True)
        sel.geometry(f"{vw}x{vh}+{vx}+{vy}")
        sel.attributes("-topmost", True)
        try:
            sel.attributes("-alpha", 0.25)
        except tk.TclError:
            pass
        sel.configure(bg="#000000")

        canvas = tk.Canvas(sel, bg="#000000", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        hint = tk.Label(
            sel, text="Drag to select a region for Claude to look at -- Esc to cancel",
            bg="#1e1e24", fg="#e8e8ec", font=("Segoe UI", 10), padx=10, pady=4,
        )
        hint.place(relx=0.5, y=24, anchor="n")

        state = {"start": None, "rect": None, "result": None}

        def on_press(event):
            state["start"] = (event.x_root, event.y_root)
            state["rect"] = canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline="#7c5cff", width=2,
            )

        def on_drag(event):
            if state["rect"] is None:
                return
            x0, y0 = state["start"]
            canvas.coords(
                state["rect"], x0 - vx, y0 - vy, event.x_root - vx, event.y_root - vy,
            )

        def finish(result):
            state["result"] = result
            sel.destroy()

        def on_release(event):
            if state["start"] is None:
                finish(None)
                return
            x0, y0 = state["start"]
            x1, y1 = event.x_root, event.y_root
            box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            # A sub-8px drag is almost certainly an accidental click, not a
            # deliberate selection -- treat it as a cancel.
            if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                finish(None)
            else:
                finish(box)

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        sel.bind("<Escape>", lambda e: finish(None))

        sel.focus_force()
        sel.grab_set()
        self.root.wait_window(sel)
        return state["result"]

    def _start_query(self, question, screenshot_path=None):
        if self.claude_exe is None:
            self._append_answer(
                "Can't find claude.exe -- is the Claude desktop app installed?",
                tag="error",
            )
            return

        label = f"You: {question}" + (" \U0001F4F7" if screenshot_path else "")
        self._append_answer(label)
        self._stopped_by_user = False
        self._set_busy(True)
        self.status_var.set("Thinking...")

        thread = threading.Thread(
            target=self._run_claude, args=(question,),
            kwargs={"screenshot_path": screenshot_path}, daemon=True,
        )
        thread.start()

    def _run_claude(self, question: str, screenshot_path=None):
        self.ui_queue.put(("status", "Checking connection..."))
        if not _internet_available():
            self.ui_queue.put((
                "answer",
                (
                    "Claude: [offline] Can't reach Claude's servers -- check "
                    "your internet connection and try again.",
                    "error",
                ),
            ))
            self.ui_queue.put(("done", None))
            return
        if self._stopped_by_user:
            self.ui_queue.put(("done", None))
            return

        # The context block goes in the per-turn PROMPT, not
        # --append-system-prompt: on a --resume call the CLI silently ignores
        # a new system prompt (it only applies when a session is first
        # created), but the -p text is genuinely fresh every single call.
        if screenshot_path:
            question = (
                f"[A screenshot was just taken and saved to {screenshot_path} -- "
                "use your Read tool to look at it before answering.]\n\n"
                f"{question}"
            )
        full_prompt = f"{build_turn_context()}\n\n{question}"
        cmd = [
            self.claude_exe,
            "-p", full_prompt,
            "--restricted",
            "--allowedTools", "WebSearch,Read",
            "--model", self.settings["model"],
            "--effort", self.settings["effort"],
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--append-system-prompt", build_system_prompt(),
        ]
        if screenshot_path:
            # Read is scoped to the process's working directory by default --
            # the screenshots folder lives under %LOCALAPPDATA%, well outside
            # that tree, so without this Read refuses it as "outside allowed
            # directory" (confirmed the hard way testing this just now).
            cmd += ["--add-dir", SCREENSHOTS_DIR]
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            cmd += ["--session-id", self.session_id]
        else:
            cmd += ["--resume", self.session_id]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.ui_queue.put(("answer", (f"Claude: [failed to run claude.exe: {exc}]", "error")))
            self.ui_queue.put(("done", None))
            return

        self._current_proc = proc
        if self._stopped_by_user:
            try:
                proc.terminate()
            except Exception:
                pass

        answer_started = False
        final_event = None
        read_error = None

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "status":
                    if event.get("status") == "requesting":
                        self.ui_queue.put(("status", "Thinking..."))

                elif etype == "stream_event":
                    inner = event.get("event", {})
                    itype = inner.get("type")

                    if itype == "content_block_start":
                        block = inner.get("content_block", {})
                        btype = block.get("type")
                        if btype == "thinking":
                            self.ui_queue.put(("status", "Thinking..."))
                        elif btype == "tool_use":
                            name = block.get("name", "a tool")
                            label = TOOL_STATUS_LABELS.get(name, f"Using {name}...")
                            self.ui_queue.put(("status", label))
                        elif btype == "text" and not answer_started:
                            answer_started = True
                            self.ui_queue.put(("answer_begin", None))

                    elif itype == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                if not answer_started:
                                    answer_started = True
                                    self.ui_queue.put(("answer_begin", None))
                                self.ui_queue.put(("answer_chunk", text))

                elif etype == "result":
                    final_event = event

            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            read_error = "claude.exe kept running after the response finished"
        except Exception as exc:
            read_error = str(exc)

        stderr_text = ""
        try:
            if proc.stderr:
                stderr_text = proc.stderr.read().strip()
        except Exception:
            pass

        self._current_proc = None

        if self._stopped_by_user:
            # A deliberate cancel, not a failure -- no alarming [error] text.
            if answer_started:
                self.ui_queue.put(("answer_chunk", "\n\n[stopped]"))
            else:
                self.ui_queue.put(("answer", ("Claude: [stopped]", None)))
            self.ui_queue.put(("done", None))
            return

        if final_event is None or final_event.get("is_error"):
            msg = (
                read_error
                or (final_event and final_event.get("result"))
                or stderr_text
                or "no response from claude.exe"
            )
            msg_lower = msg.lower()
            if "not logged in" in msg_lower or "invalid bearer" in msg_lower:
                msg += (
                    "\n\nRun `claude setup-token` in a terminal and update "
                    "CLAUDE_CODE_OAUTH_TOKEN with the new token."
                )
            elif any(p in msg_lower for p in (
                "rate limit", "rate_limit", "429", "overloaded", "usage limit",
                "quota",
            )):
                msg += (
                    "\n\nClaude is rate-limiting or over quota right now -- "
                    "wait a bit and try again."
                )
            elif any(p in msg_lower for p in (
                "network", "connection", "timed out", "timeout",
                "temporary failure", "getaddrinfo", "econnrefused", "enotfound",
            )):
                msg += "\n\nThis looks like a network problem -- check your connection and try again."
            if answer_started:
                self.ui_queue.put(("answer_chunk", f"\n\n[error] {msg}"))
            else:
                self.ui_queue.put(("answer", (f"Claude: [error] {msg}", "error")))
        elif not answer_started:
            # Nothing streamed as text (e.g. a turn that ended in pure tool
            # calls) -- fall back to the final result so there's always an answer.
            text = final_event.get("result") or "(empty response)"
            self.ui_queue.put(("answer", (f"Claude: {text}", None)))

        if final_event and not final_event.get("is_error"):
            result_text = final_event.get("result") or ""

            match = LOCATION_TAG_RE.search(result_text)
            if match:
                self.ui_queue.put(("location_found", {
                    "raw_tag": match.group(0),
                    "zone": match.group(1),
                    "x": match.group(2),
                    "y": match.group(3),
                }))

            savenote_matches = list(SAVENOTE_RE.finditer(result_text))
            if savenote_matches:
                char_name, realm, level = get_current_character_identity()
                saved_labels = []
                raw_tags = []
                for m in savenote_matches:
                    category, value = m.group(1), m.group(2)
                    if char_name and category in NOTE_CATEGORIES:
                        save_character_note(char_name, realm, category, value, level)
                        saved_labels.append(NOTE_CATEGORIES[category])
                        raw_tags.append(m.group(0))
                if saved_labels:
                    self.ui_queue.put(("notes_saved", {
                        "labels": saved_labels, "raw_tags": raw_tags,
                    }))

        self.ui_queue.put(("done", None))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:
        app = ClaudeOverlay()
        app.run()
    except Exception:
        # Catches failures before report_callback_exception is even wired up
        # (e.g. Tk itself failing to init) -- without this, a --windowed exe
        # just vanishes with no window and no console, which for a guild of
        # non-technical users looks exactly like "nothing happened."
        _log_crash(*sys.exc_info())
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                "Claude WoW Overlay hit an unexpected error and had to close.\n\n"
                f"Details were saved to:\n{CRASH_LOG_PATH}",
                "Claude WoW Overlay -- crashed",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        sys.exit(1)
