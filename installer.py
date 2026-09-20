r"""
Claude WoW Overlay -- guided setup.

A normal (non-frameless) Tkinter wizard, styled to match the overlay app's
own dark/purple look. Walks a completely non-technical user through:
  1. Confirming the Claude desktop app is installed (hard prerequisite --
     this app calls its bundled CLI directly, using the user's own login,
     which is also *why* nothing the user asks ever passes through us).
  2. Getting them logged in (claude setup-token), if not already.
  3. Finding their WoW Forever install (or letting them point at it).
  4. Copying the compiled overlay + the ClaudeContext addon into place,
     and creating a Start Menu shortcut.

Bundled payload (see payload/ next to this script, or inside the compiled
exe under sys._MEIPASS/payload/ once built with build_installer.py):
  - ClaudeWowOverlay.exe  (the compiled overlay -- no Python needed to run it)
  - icon.ico
  - ClaudeContext/ClaudeContext.toc, ClaudeContext.lua
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import winreg
from tkinter import filedialog

# ============================================================================
# Palette -- matches overlay.py exactly
# ============================================================================
BG = "#1e1e24"
BG_DARK = "#141419"
BG_FIELD = "#2a2a33"
FG = "#e8e8ec"
FG_DIM = "#9a9aa2"
ACCENT = "#7c5cff"
GREEN = "#4caf50"
RED = "#ff6b6b"

WINDOW_W, WINDOW_H = 560, 460

# Bump alongside overlay.py's APP_VERSION -- shown in Windows' "Apps &
# Features" listing via the registry uninstall entry (see register_uninstaller).
INSTALLER_VERSION = "1.1.0"

UNINSTALL_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ClaudeWowOverlay"


def resource_path(*parts):
    """Path to a bundled payload file, whether running as a plain script
    (testing) or as a PyInstaller onefile exe (sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "payload", *parts)


# ============================================================================
# Prerequisite detection (mirrors overlay.py's own logic, since this installer
# needs to answer the same "where's claude.exe" question before anything else
# can happen)
# ============================================================================

def find_claude_exe():
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
        return None

    def version_key(path):
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


def test_login(claude_exe, token=None):
    """Returns (ok, message). If token is given, tests THAT token specifically
    (used right after the user pastes one in) rather than whatever's already
    in the environment."""
    env = dict(os.environ)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    try:
        result = subprocess.run(
            [claude_exe, "-p", "Reply with exactly: OK", "--restricted",
             "--output-format", "text"],
            capture_output=True, text=True, timeout=30, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return False, str(exc)
    if result.returncode == 0 and "OK" in (result.stdout or ""):
        return True, "OK"
    return False, (result.stderr or result.stdout or "unknown error").strip()


def find_wow_forever_installs():
    """Every WoW build folder that looks like WoW Forever specifically
    (interface 16000-19999, per ClaudeContext's own VersionDetect logic),
    across both Program Files roots. Returns a list of AddOns folder paths."""
    roots = []
    for env_var, default in (("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                              ("PROGRAMFILES", r"C:\Program Files")):
        root = os.environ.get(env_var, default)
        if root and root not in roots:
            roots.append(root)

    found = []
    for root in roots:
        pattern = os.path.join(root, "World of Warcraft", "_*_")
        for build_dir in glob.glob(pattern):
            config_path = os.path.join(build_dir, "WTF", "Config.wtf")
            addons_path = os.path.join(build_dir, "Interface", "AddOns")
            if not os.path.isdir(addons_path):
                continue
            is_forever = False
            if os.path.isfile(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8", errors="replace") as f:
                        config_text = f.read()
                    m = re.search(r'lastAddonVersion\s+"(\d+)"', config_text)
                    if m and 16000 <= int(m.group(1)) <= 19999:
                        is_forever = True
                except Exception:
                    pass
            found.append((addons_path, is_forever))

    # Forever installs first
    found.sort(key=lambda pair: not pair[1])
    return found


def set_persistent_token(token):
    subprocess.run(["setx", "CLAUDE_CODE_OAUTH_TOKEN", token],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW)


# ============================================================================
# Installation actions
# ============================================================================

def register_uninstaller(install_dir, uninstall_string, icon_dst):
    """Adds an 'Apps & Features' entry so the app can be removed the normal
    Windows way, not just by deleting folders by hand. HKCU (not HKLM) --
    matches the rest of the install, which never needs admin rights."""
    try:
        size_kb = 0
        for fname in os.listdir(install_dir):
            fpath = os.path.join(install_dir, fname)
            if os.path.isfile(fpath):
                size_kb += os.path.getsize(fpath) // 1024
    except OSError:
        size_kb = 0

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Claude WoW Overlay")
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, INSTALLER_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "Claude WoW Overlay")
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, icon_dst)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_string)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        if size_kb:
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)


def install_everything(addons_path, progress_cb):
    """Copies the overlay + addon into place and creates a Start Menu
    shortcut. progress_cb(str) is called with a short status after each step.
    Returns (ok, message)."""
    try:
        install_dir = os.path.join(os.environ["LOCALAPPDATA"], "ClaudeWowOverlay")
        os.makedirs(install_dir, exist_ok=True)

        progress_cb("Copying the overlay app...")
        overlay_dst = os.path.join(install_dir, "ClaudeWowOverlay.exe")
        shutil.copy2(resource_path("ClaudeWowOverlay.exe"), overlay_dst)
        icon_dst = os.path.join(install_dir, "icon.ico")
        shutil.copy2(resource_path("icon.ico"), icon_dst)

        # Copy this same setup exe into the install dir too, purely so the
        # registry's UninstallString has something stable to point at --
        # the original download (Desktop, Downloads, wherever) might get
        # moved or deleted long before the user ever uninstalls. Skipped in
        # dev mode (running as a plain script), where sys.executable is just
        # the Python interpreter, not a real standalone copy of this tool.
        setup_exe_dst = None
        if getattr(sys, "frozen", False):
            setup_exe_dst = os.path.join(install_dir, "ClaudeWowOverlaySetup.exe")
            try:
                shutil.copy2(sys.executable, setup_exe_dst)
            except OSError:
                setup_exe_dst = None

        # Remembered so the uninstaller can find and remove the WoW addon
        # folder too, without re-asking the user where WoW is installed.
        try:
            with open(os.path.join(install_dir, "install_info.json"), "w", encoding="utf-8") as f:
                json.dump({"addons_path": addons_path}, f)
        except OSError:
            pass

        if setup_exe_dst:
            try:
                register_uninstaller(
                    install_dir, f'"{setup_exe_dst}" --uninstall', icon_dst,
                )
            except OSError:
                pass  # a missing "Apps & Features" entry shouldn't fail the install

        if addons_path:
            progress_cb("Installing the WoW addon...")
            addon_dst = os.path.join(addons_path, "ClaudeContext")
            os.makedirs(addon_dst, exist_ok=True)
            for fname in ("ClaudeContext.toc", "ClaudeContext.lua"):
                shutil.copy2(resource_path("ClaudeContext", fname),
                             os.path.join(addon_dst, fname))

        progress_cb("Creating Start Menu shortcut...")
        shortcut_path = os.path.join(
            os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu",
            "Programs", "Claude WoW Overlay.lnk",
        )
        ps_script = f'''
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("{shortcut_path}")
$shortcut.TargetPath = "{overlay_dst}"
$shortcut.WorkingDirectory = "{install_dir}"
$shortcut.IconLocation = "{icon_dst}"
$shortcut.Description = "Floating Claude Q&A box for WoW -- Ctrl+Shift+Space to show/hide"
$shortcut.Save()
'''
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )

        progress_cb("Done!")
        return True, install_dir
    except Exception as exc:
        return False, str(exc)


def launch_setup_token_terminal(claude_exe):
    """Opens a real, visible console window running `claude setup-token`, so
    the user can complete the browser approval and see/copy the printed
    token. This has to be a real terminal -- setup-token draws its own UI
    and produces no output at all when piped (confirmed the hard way while
    building the overlay itself)."""
    subprocess.Popen(
        ["cmd", "/c", "start", "Claude Setup", "cmd", "/k", claude_exe, "setup-token"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


# ============================================================================
# UI
# ============================================================================

class InstallerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Claude WoW Overlay Setup")
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

        # center on screen
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"+{(sw - WINDOW_W) // 2}+{(sh - WINDOW_H) // 3}")

        self.claude_exe = None
        self.selected_addons_path = None
        self.wow_installs = []

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill="both", expand=True)

        self._page_welcome()

    def _clear(self):
        for widget in self.content.winfo_children():
            widget.destroy()

    def _header(self, title, subtitle=""):
        tk.Label(
            self.content, text=title, bg=BG, fg=FG,
            font=("Segoe UI", 16, "bold"), anchor="w",
        ).pack(fill="x", padx=32, pady=(32, 4))
        if subtitle:
            tk.Label(
                self.content, text=subtitle, bg=BG, fg=FG_DIM,
                font=("Segoe UI", 10), anchor="w", justify="left", wraplength=496,
            ).pack(fill="x", padx=32, pady=(0, 16))

    def _button(self, parent, text, command, primary=True):
        bg = ACCENT if primary else BG_FIELD
        fg = "white" if primary else FG
        btn = tk.Label(
            parent, text=text, bg=bg, fg=fg, font=("Segoe UI", 10, "bold"),
            padx=18, pady=8, cursor="hand2",
        )
        btn.bind("<Button-1>", lambda e: command())
        return btn

    def _privacy_box(self, parent):
        box = tk.Frame(parent, bg=BG_DARK)
        box.pack(fill="x", padx=32, pady=(0, 16))
        tk.Label(
            box, text="\U0001F512  Your questions go straight to YOUR OWN Claude "
            "account. Nothing passes through us -- we never see what you ask "
            "or what Claude replies.",
            bg=BG_DARK, fg=FG, font=("Segoe UI", 9), justify="left",
            wraplength=470, padx=14, pady=12,
        ).pack(fill="x")

    # ---------- Page 1: Welcome ----------

    def _page_welcome(self):
        self._clear()
        self._header(
            "Claude WoW Overlay",
            "A floating AI assistant for World of Warcraft: Forever -- press "
            "Ctrl+Shift+Space anytime to ask it about quests, zones, "
            "professions, or anything else, and it knows your character, "
            "location and quest log automatically.",
        )
        self._privacy_box(self.content)

        steps = tk.Frame(self.content, bg=BG)
        steps.pack(fill="x", padx=32, pady=(0, 16))
        for i, text in enumerate([
            "Check that everything needed is installed",
            "Connect your Claude account (one-time, in your browser)",
            "Install the app and the WoW addon",
        ], start=1):
            row = tk.Frame(steps, bg=BG)
            row.pack(fill="x", pady=4)
            tk.Label(
                row, text=str(i), bg=ACCENT, fg="white",
                font=("Segoe UI", 9, "bold"), width=2,
            ).pack(side="left")
            tk.Label(
                row, text=text, bg=BG, fg=FG, font=("Segoe UI", 10),
                anchor="w", padx=10,
            ).pack(side="left", fill="x")

        footer = tk.Frame(self.content, bg=BG)
        footer.pack(side="bottom", fill="x", padx=32, pady=24)
        self._button(footer, "Get Started  \u2192", self._page_checking).pack(side="right")

    # ---------- Page 2: Checking prerequisites ----------

    def _page_checking(self):
        self._clear()
        self._header("Checking your setup...")

        self.check_rows = {}
        checks_frame = tk.Frame(self.content, bg=BG)
        checks_frame.pack(fill="x", padx=32, pady=8)
        for key, label in [
            ("claude", "Claude desktop app"),
            ("login", "Signed in to Claude"),
            ("wow", "World of Warcraft: Forever"),
        ]:
            row = tk.Frame(checks_frame, bg=BG)
            row.pack(fill="x", pady=6)
            status_lbl = tk.Label(row, text="\u22EF", bg=BG, fg=FG_DIM,
                                   font=("Segoe UI", 12), width=3)
            status_lbl.pack(side="left")
            tk.Label(row, text=label, bg=BG, fg=FG, font=("Segoe UI", 11),
                     anchor="w").pack(side="left", fill="x", expand=True)
            self.check_rows[key] = status_lbl

        self.check_detail = tk.Label(
            self.content, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
            wraplength=496, justify="left", anchor="w",
        )
        self.check_detail.pack(fill="x", padx=32, pady=(8, 0))

        threading.Thread(target=self._run_checks, daemon=True).start()

    def _set_check(self, key, ok):
        symbol = "\u2713" if ok else "\u2717"
        color = GREEN if ok else RED
        self.root.after(0, lambda: self.check_rows[key].config(text=symbol, fg=color))

    def _run_checks(self):
        self.claude_exe = find_claude_exe()
        self._set_check("claude", self.claude_exe is not None)

        logged_in = False
        if self.claude_exe:
            logged_in, _ = test_login(self.claude_exe)
        self._set_check("login", logged_in)

        self.wow_installs = find_wow_forever_installs()
        self._set_check("wow", len(self.wow_installs) > 0)

        self.root.after(600, lambda: self._checks_done(logged_in))

    def _checks_done(self, logged_in):
        if not self.claude_exe:
            self._page_need_claude_app()
        elif not logged_in:
            self._page_login()
        else:
            self._page_wow_confirm()

    # ---------- Page 2a: missing Claude app ----------

    def _page_need_claude_app(self):
        self._clear()
        self._header(
            "Claude desktop app not found",
            "This overlay needs the official Claude desktop app installed "
            "first -- it uses its bundled CLI and your existing login, which "
            "is also what keeps your data private.",
        )
        tk.Label(
            self.content,
            text="Install it from claude.ai, then run this setup again.",
            bg=BG, fg=FG, font=("Segoe UI", 10), wraplength=496,
            justify="left", anchor="w",
        ).pack(fill="x", padx=32, pady=8)

        footer = tk.Frame(self.content, bg=BG)
        footer.pack(side="bottom", fill="x", padx=32, pady=24)
        self._button(footer, "Check Again", self._page_checking).pack(side="right")
        self._button(footer, "Close", self.root.destroy, primary=False).pack(side="right", padx=(0, 8))

    # ---------- Page 2b: login guide ----------

    def _page_login(self):
        self._clear()
        self._header(
            "Connect your Claude account",
            "One-time setup, done entirely in your own browser -- this app "
            "never sees your password or session.",
        )

        steps = [
            "Click \u201cOpen Setup\u201d below. A window will pop up.",
            "It opens your browser -- sign in and approve.",
            "The window prints a long code starting with sk-ant-oat01-. "
            "Copy the whole thing.",
            "Paste it in the box below and click Verify.",
        ]
        for i, text in enumerate(steps, start=1):
            row = tk.Frame(self.content, bg=BG)
            row.pack(fill="x", padx=32, pady=3)
            tk.Label(row, text=f"{i}.", bg=BG, fg=ACCENT,
                     font=("Segoe UI", 10, "bold"), width=2, anchor="w").pack(side="left")
            tk.Label(row, text=text, bg=BG, fg=FG, font=("Segoe UI", 10),
                     anchor="w", justify="left", wraplength=460).pack(side="left", fill="x")

        open_row = tk.Frame(self.content, bg=BG)
        open_row.pack(fill="x", padx=32, pady=(12, 4))
        self._button(open_row, "Open Setup", self._open_setup_token).pack(side="left")

        paste_row = tk.Frame(self.content, bg=BG)
        paste_row.pack(fill="x", padx=32, pady=(12, 4))
        self.token_entry = tk.Entry(
            paste_row, bg=BG_FIELD, fg=FG, insertbackground=FG,
            font=("Segoe UI", 10), bd=0,
        )
        self.token_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))

        self.login_status = tk.Label(
            self.content, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
            anchor="w", wraplength=496, justify="left",
        )
        self.login_status.pack(fill="x", padx=32, pady=(4, 0))

        footer = tk.Frame(self.content, bg=BG)
        footer.pack(side="bottom", fill="x", padx=32, pady=24)
        self._button(footer, "Verify \u2192", self._verify_token).pack(side="right")
        self._button(footer, "Skip for now", self._page_wow_confirm, primary=False).pack(side="right", padx=(0, 8))

    def _open_setup_token(self):
        launch_setup_token_terminal(self.claude_exe)
        self.login_status.config(text="A window opened -- come back here once you've copied the code.", fg=FG_DIM)

    def _verify_token(self):
        token = self.token_entry.get().strip()
        if not token:
            self.login_status.config(text="Paste the code first.", fg=RED)
            return
        self.login_status.config(text="Checking...", fg=FG_DIM)
        self.root.update_idletasks()
        ok, msg = test_login(self.claude_exe, token=token)
        if ok:
            set_persistent_token(token)
            self.login_status.config(text="Connected!", fg=GREEN)
            self.root.after(500, self._page_wow_confirm)
        else:
            self.login_status.config(
                text=f"That didn't work ({msg}). Double-check you copied the "
                     "whole code and try again.",
                fg=RED,
            )

    # ---------- Page 3: confirm / pick WoW install ----------

    def _page_wow_confirm(self):
        self._clear()
        if self.wow_installs:
            self.selected_addons_path = self.wow_installs[0][0]
            self._header(
                "Found World of Warcraft: Forever",
                self.selected_addons_path.replace("\\Interface\\AddOns", ""),
            )
            footer = tk.Frame(self.content, bg=BG)
            footer.pack(side="bottom", fill="x", padx=32, pady=24)
            self._button(footer, "Install \u2192", self._page_installing).pack(side="right")
            self._button(footer, "Choose a different folder", self._browse_wow,
                          primary=False).pack(side="right", padx=(0, 8))
        else:
            self._header(
                "Couldn't find WoW Forever automatically",
                "The overlay app itself will still work either way -- this "
                "only affects whether it can see your character/quest data. "
                "You can point at your WoW folder manually, or skip this and "
                "add the addon yourself later.",
            )
            footer = tk.Frame(self.content, bg=BG)
            footer.pack(side="bottom", fill="x", padx=32, pady=24)
            self._button(footer, "Install without addon \u2192",
                          self._page_installing).pack(side="right")
            self._button(footer, "Browse...", self._browse_wow,
                          primary=False).pack(side="right", padx=(0, 8))

    def _browse_wow(self):
        folder = filedialog.askdirectory(title="Select your World of Warcraft folder")
        if not folder:
            return
        addons_path = os.path.join(folder, "Interface", "AddOns")
        if os.path.isdir(addons_path):
            self.selected_addons_path = addons_path
            self._page_installing()
        else:
            self.check_detail_browse_error = True
            self._header("That doesn't look like a WoW folder", "")

    # ---------- Page 4: installing ----------

    def _page_installing(self):
        self._clear()
        self._header("Installing...")
        self.install_status = tk.Label(
            self.content, text="Starting...", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 10), anchor="w",
        )
        self.install_status.pack(fill="x", padx=32, pady=16)
        threading.Thread(target=self._do_install, daemon=True).start()

    def _do_install(self):
        def progress(text):
            self.root.after(0, lambda: self.install_status.config(text=text))

        ok, result = install_everything(self.selected_addons_path, progress)
        self.root.after(300, lambda: self._page_done(ok, result))

    # ---------- Page 5: done ----------

    def _page_done(self, ok, result):
        self._clear()
        if not ok:
            self._header("Something went wrong", result)
            footer = tk.Frame(self.content, bg=BG)
            footer.pack(side="bottom", fill="x", padx=32, pady=24)
            self._button(footer, "Close", self.root.destroy).pack(side="right")
            return

        self._header("All set!", "Press Ctrl+Shift+Space anytime to open the box.")

        tip = tk.Frame(self.content, bg=BG_DARK)
        tip.pack(fill="x", padx=32, pady=(0, 16))
        tk.Label(
            tip, text="To pin it to your taskbar: press the Windows key, type "
            "\u201cClaude WoW Overlay\u201d, right-click it, and choose "
            "\u201cPin to taskbar\u201d.",
            bg=BG_DARK, fg=FG, font=("Segoe UI", 9), justify="left",
            wraplength=470, padx=14, pady=12,
        ).pack(fill="x")

        if not self.selected_addons_path:
            tk.Label(
                self.content,
                text="Note: the WoW addon wasn't installed since your game "
                     "folder wasn't found. The overlay itself works fine "
                     "without it -- it just won't know your character/quests.",
                bg=BG, fg=FG_DIM, font=("Segoe UI", 9), wraplength=496,
                justify="left", anchor="w",
            ).pack(fill="x", padx=32, pady=(0, 8))

        footer = tk.Frame(self.content, bg=BG)
        footer.pack(side="bottom", fill="x", padx=32, pady=24)
        self._button(footer, "Launch Now", self._launch_and_close).pack(side="right")
        self._button(footer, "Close", self.root.destroy, primary=False).pack(side="right", padx=(0, 8))

    def _launch_and_close(self):
        exe = os.path.join(os.environ["LOCALAPPDATA"], "ClaudeWowOverlay", "ClaudeWowOverlay.exe")
        try:
            subprocess.Popen([exe])
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ============================================================================
# Uninstall -- same exe, launched with --uninstall (that's what the registry
# entry's UninstallString points at, so "Apps & Features" -> Uninstall just
# works without a separate download).
# ============================================================================

def do_uninstall(progress_cb, install_dir=None, shortcut_path=None):
    """Removes everything install_everything() put in place, except the
    install directory itself and this running exe -- those are deleted by a
    short detached command scheduled to run just after this process exits
    (deleting a file/folder a running exe's own image is still open from can
    fail partway through otherwise). progress_cb(str) reports status.

    install_dir/shortcut_path default to the real production paths; the
    parameters exist so tests can point this at disposable temp paths
    instead -- this function kills a running ClaudeWowOverlay.exe by name
    and recursively deletes a whole folder, so it must never run against
    real paths outside of an actual uninstall."""
    if install_dir is None:
        install_dir = os.path.join(os.environ["LOCALAPPDATA"], "ClaudeWowOverlay")
    if shortcut_path is None:
        shortcut_path = os.path.join(
            os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu",
            "Programs", "Claude WoW Overlay.lnk",
        )

    progress_cb("Stopping the overlay if it's running...")
    subprocess.run(
        ["taskkill", "/IM", "ClaudeWowOverlay.exe", "/F"],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )

    progress_cb("Removing the WoW addon...")
    try:
        with open(os.path.join(install_dir, "install_info.json"), "r", encoding="utf-8") as f:
            addons_path = json.load(f).get("addons_path")
        if addons_path:
            addon_dir = os.path.join(addons_path, "ClaudeContext")
            if os.path.isdir(addon_dir):
                shutil.rmtree(addon_dir, ignore_errors=True)
    except (OSError, json.JSONDecodeError):
        pass

    progress_cb("Removing the Start Menu shortcut...")
    try:
        if os.path.isfile(shortcut_path):
            os.remove(shortcut_path)
    except OSError:
        pass

    progress_cb("Removing settings...")
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_KEY)
    except OSError:
        pass

    # This exe is itself a file inside install_dir and still running --
    # rmdir it directly and it can fail partway through. Instead, hand off to
    # a detached `cmd` that waits a couple seconds (long enough for this
    # process to fully exit and release its file handle) and then removes
    # the whole folder, itself included. Standard self-deleting-installer
    # trick; no admin rights needed since it's all under %LOCALAPPDATA%.
    progress_cb("Finishing up...")
    try:
        subprocess.Popen(
            ["cmd", "/c", "timeout", "/t", "2", "/nobreak", ">nul",
             "&", "rmdir", "/s", "/q", install_dir],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
    except OSError:
        pass


class UninstallApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Uninstall Claude WoW Overlay")
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"+{(sw - WINDOW_W) // 2}+{(sh - WINDOW_H) // 3}")

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill="both", expand=True)

        self._page_confirm()

    def _clear(self):
        for widget in self.content.winfo_children():
            widget.destroy()

    def _header(self, title, subtitle=""):
        tk.Label(
            self.content, text=title, bg=BG, fg=FG,
            font=("Segoe UI", 16, "bold"), anchor="w",
        ).pack(fill="x", padx=32, pady=(32, 4))
        if subtitle:
            tk.Label(
                self.content, text=subtitle, bg=BG, fg=FG_DIM,
                font=("Segoe UI", 10), anchor="w", justify="left", wraplength=496,
            ).pack(fill="x", padx=32, pady=(0, 16))

    def _button(self, parent, text, command, primary=True):
        bg = ACCENT if primary else BG_FIELD
        fg = "white" if primary else FG
        btn = tk.Label(
            parent, text=text, bg=bg, fg=fg, font=("Segoe UI", 10, "bold"),
            padx=18, pady=8, cursor="hand2",
        )
        btn.bind("<Button-1>", lambda e: command())
        return btn

    def _page_confirm(self):
        self._clear()
        self._header(
            "Uninstall Claude WoW Overlay?",
            "This removes the app, the WoW addon, your Start Menu shortcut, "
            "and your saved settings/notes. This can't be undone.",
        )
        footer = tk.Frame(self.content, bg=BG)
        footer.pack(side="bottom", fill="x", padx=32, pady=24)
        self._button(footer, "Uninstall", self._page_uninstalling).pack(side="right")
        self._button(footer, "Cancel", self.root.destroy, primary=False).pack(side="right", padx=(0, 8))

    def _page_uninstalling(self):
        self._clear()
        self._header("Uninstalling...")
        self.status_lbl = tk.Label(
            self.content, text="Starting...", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 10), anchor="w",
        )
        self.status_lbl.pack(fill="x", padx=32, pady=16)
        threading.Thread(target=self._do, daemon=True).start()

    def _do(self):
        def progress(text):
            self.root.after(0, lambda: self.status_lbl.config(text=text))
        do_uninstall(progress)
        self.root.after(300, self._page_done)

    def _page_done(self):
        self._clear()
        self._header("Uninstalled", "Claude WoW Overlay has been removed. Thanks for trying it out.")
        footer = tk.Frame(self.content, bg=BG)
        footer.pack(side="bottom", fill="x", padx=32, pady=24)
        self._button(footer, "Close", self.root.destroy).pack(side="right")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if "--uninstall" in sys.argv[1:]:
        UninstallApp().run()
    else:
        InstallerApp().run()
