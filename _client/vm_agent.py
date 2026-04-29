"""
vm_agent.py — CS2 Farm VM Agent v3.0
HTTP server (port 9999) на каждой VM (деплой из папки _client/).

Поток входа в Steam (GUI + AutoIt):
  1. Закрыть процессы Steam
  2. Запуск Steam с опциями из FSM_STEAM_LAUNCH_OPTIONS
  3. AutoIt: ввод логина/пароля, Steam Guard (TOTP)
  4. /cs2/launch — запуск CS2

Рядом со скриптом: autoit/lib/AutoItX3_x64.dll, settings/cs2_machine_convars.vcfg,
get_guard_code.js, cs2_lobby.js; npm install для Node-скриптов.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 9999
LOG_FILE = Path(__file__).parent / "vm_agent.log"

# Макс. секунд ждём завершения входа за одну попытку; после таймаута — kill Steam и повтор.
STEAM_LOGIN_TIMEOUT = 20
STEAM_LOGIN_ATTEMPTS = 2

# Опции запуска Steam (старый клиент / вебхелперы)
FSM_STEAM_LAUNCH_OPTIONS = (
    "-nofriendsui -noverifyfiles -nobootstrapupdate "
    "-skipinitialbootstrap -norepairfiles -overridepackageurl "
    "-disable-winh264 -language english"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vm_agent")


# ---------------------------------------------------------------------------
# AutoIt loader — AutoItX3_x64.dll в autoit/lib/ рядом со скриптом
# ---------------------------------------------------------------------------

def _load_autoit():
    """Загрузка AutoItX через ctypes из autoit/lib/."""
    dll_candidates = [
        Path(__file__).resolve().parent / "autoit" / "lib" / "AutoItX3_x64.dll",
        Path(__file__).resolve().parent / "autoit" / "lib" / "AutoItX3.dll",
        Path(__file__).resolve().parent / "autoit" / "lib" / "AutoItX3_64.dll",
        Path("C:/CS2_FARM/autoit/lib/AutoItX3_x64.dll"),
        Path("C:/CS2_FARM/autoit/lib/AutoItX3.dll"),
        Path("Z:/C/CS2_FARM/autoit/lib/AutoItX3_x64.dll"),
        Path("Z:/C/CS2_FARM/autoit/lib/AutoItX3.dll"),
    ]

    for dll_path in dll_candidates:
        if not dll_path.exists():
            continue
        try:
            import ctypes
            lib = ctypes.cdll.LoadLibrary(str(dll_path))
            log.info("AutoIt: DLL loaded from %s", dll_path)

            class _AutoIt:
                def __init__(self, lib):
                    self._lib = lib

                def _w(self, s):
                    import ctypes
                    return ctypes.c_wchar_p(s)

                def _i(self, n):
                    import ctypes
                    return ctypes.c_int(n)

                def win_wait(self, title: str, timeout: int = 30) -> int:
                    return self._lib.AU3_WinWait(self._w(title), self._w(""), self._i(timeout))

                def win_wait_active(self, title: str, timeout: int = 30) -> int:
                    return self._lib.AU3_WinWaitActive(self._w(title), self._w(""), self._i(timeout))

                def win_activate(self, title: str) -> None:
                    self._lib.AU3_WinActivate(self._w(title), self._w(""))

                def win_exists(self, title: str) -> bool:
                    return bool(self._lib.AU3_WinExists(self._w(title), self._w("")))

                def control_set_text(self, title: str, text: str, control: str, value: str) -> int:
                    return self._lib.AU3_ControlSetText(
                        self._w(title), self._w(text), self._w(control), self._w(value)
                    )

                def control_click(self, title: str, text: str, control: str,
                                   button: str = "left", clicks: int = 1) -> int:
                    import ctypes
                    return self._lib.AU3_ControlClick(
                        self._w(title), self._w(text), self._w(control),
                        self._w(button), self._i(clicks), self._i(-1), self._i(-1)
                    )

                def control_focus(self, title: str, text: str, control: str) -> int:
                    return self._lib.AU3_ControlFocus(self._w(title), self._w(text), self._w(control))

                def send(self, keys: str, flag: int = 0) -> None:
                    import ctypes
                    self._lib.AU3_Send(self._w(keys), ctypes.c_int(flag))

                def control_send(self, title: str, text: str, control: str,
                                  keys: str, flag: int = 0) -> int:
                    import ctypes
                    return self._lib.AU3_ControlSend(
                        self._w(title), self._w(text), self._w(control),
                        self._w(keys), ctypes.c_int(flag)
                    )

                def set_option(self, option: str, value: int) -> int:
                    return self._lib.AU3_AutoItSetOption(self._w(option), self._i(value))

                def win_close(self, title: str) -> None:
                    self._lib.AU3_WinClose(self._w(title), self._w(""))

                def win_get_pos(self, title: str) -> tuple[int, int, int, int]:
                    """Returns (x, y, w, h) of the window in screen coordinates.
                    Returns (-1, -1, -1, -1) if window not found."""
                    import ctypes

                    class RECT(ctypes.Structure):
                        _fields_ = [
                            ("left",   ctypes.c_long),
                            ("top",    ctypes.c_long),
                            ("right",  ctypes.c_long),
                            ("bottom", ctypes.c_long),
                        ]

                    rect = RECT(-1, -1, -1, -1)
                    self._lib.AU3_WinGetPos(self._w(title), self._w(""), ctypes.byref(rect))
                    if rect.left == -1:
                        return (-1, -1, -1, -1)
                    return (rect.left, rect.top,
                            rect.right - rect.left, rect.bottom - rect.top)

                def mouse_click(self, button: str, x: int, y: int,
                                 clicks: int = 1, speed: int = 3) -> int:
                    """Click at absolute screen coordinates."""
                    import ctypes
                    return self._lib.AU3_MouseClick(
                        self._w(button), ctypes.c_int(x), ctypes.c_int(y),
                        ctypes.c_int(clicks), ctypes.c_int(speed),
                    )

                def mouse_move(self, x: int, y: int, speed: int = 3) -> int:
                    """Move mouse to absolute screen coordinates without clicking."""
                    import ctypes
                    return self._lib.AU3_MouseMove(
                        ctypes.c_int(x), ctypes.c_int(y), ctypes.c_int(speed),
                    )

            return _AutoIt(lib)
        except Exception as exc:
            log.warning("AutoIt DLL load failed (%s): %s", dll_path.name, exc)

    log.error(
        "AutoIt недоступен — положите AutoItX3_x64.dll в autoit/lib/ рядом с vm_agent.py "
        "(или C:\\CS2_FARM\\autoit\\lib\\)"
    )
    return None


_autoit_instance = None


def get_autoit():
    global _autoit_instance
    if _autoit_instance is None:
        _autoit_instance = _load_autoit()
    return _autoit_instance


# ---------------------------------------------------------------------------
# Steam TOTP — пакет steam-totp (Node) или запасной расчёт на Python
# ---------------------------------------------------------------------------

def generate_steam_totp(shared_secret: str) -> str:
    """TOTP Steam Guard: сначала node + get_guard_code.js, иначе Python."""
    # Try Node.js steam-totp first (authoritative)
    script = _find_script("get_guard_code.js")
    node = "node"
    try:
        result = subprocess.run(
            [node, script, shared_secret],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(script).parent),
        )
        if result.returncode == 0:
            code = result.stdout.strip()
            if len(code) == 5:
                return code
            log.warning("get_guard_code.js unexpected output: %r", code)
    except Exception as exc:
        log.warning("Node.js TOTP failed (%s), falling back to Python", exc)

    # Python fallback (RFC 6238 / Steam algorithm)
    key = base64.b64decode(shared_secret)
    msg = struct.pack(">Q", int(time.time()) // 30)
    mac = hmac.new(key, msg, hashlib.sha1).digest()
    offset = mac[19] & 0x0F
    code_int = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    chars = "23456789BCDFGHJKMNPQRTVWXY"
    result = ""
    for _ in range(5):
        result += chars[code_int % len(chars)]
        code_int //= len(chars)
    return result


# AutoIt special chars that must be escaped in send() calls
_AUTOIT_ESCAPE_MAP = {
    '!': '{!}', '^': '{^}', '+': '{+}', '#': '{#}',
    '{': '{{}', '}': '{}}',
}


def _send_text(ai, text: str, char_delay: float = 0.06) -> None:
    """Send text character by character with delay, escaping AutoIt special chars.
    Works with new Steam React UI where there are no standard Win32 Edit controls."""
    for ch in text:
        ai.send(_AUTOIT_ESCAPE_MAP.get(ch, ch))
        time.sleep(char_delay)


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------

class AgentState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: str = "idle"
        self.steamid: str = ""
        self.login: str = ""
        self.error: str = ""
        self.steam_root: str = ""
        self.cs2_path: str = ""
        self._steam_proc: subprocess.Popen | None = None
        self._cs2_proc: subprocess.Popen | None = None
        # Console log match detection
        self.match_id: str = ""
        self.match_found_at: str = ""
        self.console_extra: dict = {}
        self._tail_stop: threading.Event = threading.Event()

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            out = {
                "state": self.state,
                "steamid": self.steamid,
                "login": self.login,
                "error": self.error,
            }
            if self.match_id:
                out["match_id"] = self.match_id
                out["match_found_at"] = self.match_found_at
                out["console_extra"] = dict(self.console_extra)
            return out


STATE = AgentState()


# ---------------------------------------------------------------------------
# Steam helpers
# ---------------------------------------------------------------------------

def kill_steam() -> None:
    """Завершить процессы Steam перед логином."""
    for proc_name in ("steam.exe", "steamwebhelper.exe"):
        subprocess.run(["taskkill", "/F", "/IM", proc_name], capture_output=True)
    time.sleep(3)


def _find_script(name: str) -> str:
    base = Path(__file__).resolve().parent
    for p in (base / name, Path("C:/CS2_FARM") / name):
        if p.exists():
            return str(p)
    return name


# ---------------------------------------------------------------------------
# Steam GUI login (AutoIt + окно «Steam»)
# ---------------------------------------------------------------------------

def steam_login_fsm(login: str, password: str, shared_secret: str,
                     steam_path: str, extra_launch_opts: str = "") -> bool:
    """Вход через GUI Steam; до STEAM_LOGIN_ATTEMPTS попыток."""
    ai = get_autoit()
    if ai is None:
        log.error("Cannot login without AutoIt. Install pyautoit or copy AutoItX3_x64.dll")
        return False

    for attempt in range(1, STEAM_LOGIN_ATTEMPTS + 1):
        log.info("Steam login attempt %d/%d for %s", attempt, STEAM_LOGIN_ATTEMPTS, login)
        kill_steam()

        # Patch cs2_video.txt BEFORE Steam starts — Steam reads it on launch,
        # so writing it here (while Steam is dead) guarantees no overwrites.
        patch_cs2_video_config(str(Path(steam_path).parent))

        # Launch Steam without -login flag — let AutoIt type credentials into the UI
        launch_cmd = (
            f'"{steam_path}" {FSM_STEAM_LAUNCH_OPTIONS} {extra_launch_opts}'
        )
        log.info("Launching Steam: %s", launch_cmd[:120])
        proc = subprocess.Popen(launch_cmd, shell=True)
        STATE._steam_proc = proc

        # --- Wait for Steam login window ---
        # New Steam UI title is "Steam" (React-based, no standard Win32 Edit controls).
        # Login field is auto-focused by Steam when the window opens.
        log.info("Waiting for Steam login window...")
        result = ai.win_wait("Steam", timeout=30)
        if not result:
            log.warning("Steam window not appeared in 30s")
            continue

        time.sleep(10)  # let Steam fully render its React UI and load login page
        ai.win_activate("Steam")
        time.sleep(1.5)  # wait for window activation + focus settle

        # --- Type login (field already focused by Steam on open) ---
        log.info("Typing login: %s", login)
        _send_text(ai, login)
        time.sleep(0.5)

        # --- Tab to password field ---
        ai.send("{TAB}")
        time.sleep(0.8)

        # --- Type password ---
        log.info("Typing password")
        _send_text(ai, password)
        time.sleep(0.4)

        # --- Submit ---
        ai.send("{ENTER}")
        log.info("Pressed Enter — Sign In submitted")

        # --- Steam Guard (new Steam UI) ---
        # In the new Steam React UI the Guard prompt appears inside the same "Steam" window,
        # so window title stays "Steam". We wait for the Guard screen to load,
        # then type the TOTP — the code input field is auto-focused just like the login field.
        if shared_secret:
            log.info("Waiting 5s for Steam Guard screen to appear...")
            time.sleep(5)

            # Generate TOTP as late as possible.
            # If we're in the last 6 seconds of the 30-second window, wait for the next
            # fresh code — otherwise the code might expire before Steam processes it.
            seconds_in_window = time.time() % 30
            if seconds_in_window > 24:
                wait_sec = 30 - seconds_in_window + 1
                log.info("Near TOTP window boundary (%.1fs left) — waiting %.1fs for fresh code",
                         30 - seconds_in_window, wait_sec)
                time.sleep(wait_sec)

            totp = generate_steam_totp(shared_secret)
            now = time.time()
            seconds_remaining = 30 - (now % 30)
            import datetime
            log.info("TOTP code: %s  (%.0fs remaining in window, VM UTC time: %s)",
                     totp, seconds_remaining,
                     datetime.datetime.utcfromtimestamp(now).strftime("%H:%M:%S"))

            ai.win_activate("Steam")
            time.sleep(1.0)
            _send_text(ai, totp, char_delay=0.12)
            time.sleep(0.4)
            ai.send("{ENTER}")
            log.info("Steam Guard code submitted")
        else:
            log.info("No shared_secret — skipping Steam Guard step")

        # --- Detect login success: steam.exe running + no login/guard dialogs ---
        # NOTE: proc.poll() is NOT used — Steam launcher exits immediately after
        # starting the main client process. Use tasklist to check steam.exe.
        log.info("Waiting for Steam to finish login (up to %ds)...", STEAM_LOGIN_TIMEOUT)
        deadline = time.time() + STEAM_LOGIN_TIMEOUT
        logged_in = False
        while time.time() < deadline:
            # Check steam.exe is running via tasklist (launcher may have exited)
            tl = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
                                capture_output=True, text=True)
            steam_running = "steam.exe" in tl.stdout

            # In new Steam UI the Guard prompt is inside the main "Steam" window.
            # We detect "still guarding" by checking if steam.exe is running but
            # no Friends/library window has appeared yet — checked via login_open below.
            guard_open = False
            login_open = ai.win_exists("Sign in to Steam")

            if steam_running and not guard_open and not login_open:
                # Steam is running and no login dialogs — login complete
                logged_in = True
                break

            if not steam_running:
                log.warning("steam.exe not found in tasklist — process may have crashed")
                break

            time.sleep(3)

        if logged_in:
            log.info("Steam login successful for %s", login)
            return True

        log.warning("Login attempt %d failed — retrying", attempt)
        kill_steam()
        time.sleep(5)

    log.error("All %d login attempts failed for %s", STEAM_LOGIN_ATTEMPTS, login)
    return False


# ---------------------------------------------------------------------------
# CS2 launch — опции производительности/окна
# ---------------------------------------------------------------------------

FSM_CS2_LAUNCH_OPTIONS = (
    "-swapcores -noqueuedload -vrdisable -windowed -nopreload "
    "-limitvsconst -softparticlesdefaultoff -nohltv -noaafonts -nosound "
    "-novid +violence_hblood 0 +sethdmodels 0 +mat_disable_fancy_blending 1 "
    "+r_dynamic 0 +engine_no_focus_sleep 120 +fps_max 40"
)

# Target resolution for farm windows — must match patch_cs2_video_config defaults
CS2_FARM_WIDTH = 1080
CS2_FARM_HEIGHT = 810


def _inject_resolution_args(launch_opts: str) -> str:
    """Append -w/-h/-windowed to cs2 launch args if not already present.
    Command-line args override cs2_video.txt and are not affected by Steam Cloud sync."""
    base = launch_opts or FSM_CS2_LAUNCH_OPTIONS
    # Remove any existing -w/-h flags to avoid duplicates
    import re
    base = re.sub(r"-w\s+\d+\b", "", base)
    base = re.sub(r"-h\s+\d+\b", "", base)
    base = base.strip()
    result = f"{base} -w {CS2_FARM_WIDTH} -h {CS2_FARM_HEIGHT} -windowed"
    log.info("CS2 launch opts with resolution: %s", result[:120])
    return result


_CS2_VIDEO_TEMPLATE = """\
"video.cfg"
{{
\t"Version"\t\t"16"
\t"VendorID"\t\t"4318"
\t"DeviceID"\t\t"10115"
\t"setting.cpu_level"\t\t"0"
\t"setting.gpu_mem_level"\t\t"0"
\t"setting.gpu_level"\t\t"0"
\t"setting.knowndevice"\t\t"1"
\t"setting.defaultres"\t\t"{width}"
\t"setting.defaultresheight"\t\t"{height}"
\t"setting.refreshrate_numerator"\t\t"0"
\t"setting.refreshrate_denominator"\t\t"0"
\t"setting.fullscreen"\t\t"0"
\t"setting.coop_fullscreen"\t\t"0"
\t"setting.nowindowborder"\t\t"1"
\t"setting.mat_vsync"\t\t"0"
\t"setting.fullscreen_min_on_focus_loss"\t\t"1"
\t"setting.high_dpi"\t\t"0"
\t"Autoconfig"\t\t"2"
\t"setting.shaderquality"\t\t"0"
\t"setting.r_texturefilteringquality"\t\t"0"
\t"setting.msaa_samples"\t\t"0"
\t"setting.r_csgo_cmaa_enable"\t\t"0"
\t"setting.r_low_latency"\t\t"0"
\t"setting.aspectratiomode"\t\t"0"
\t"setting.videocfg_texture_detail"\t\t"0"
\t"setting.videocfg_shadow_quality"\t\t"0"
\t"setting.videocfg_ao_detail"\t\t"0"
\t"setting.videocfg_particle_detail"\t\t"0"
\t"setting.videocfg_fsr_detail"\t\t"4"
\t"setting.videocfg_hdr_detail"\t\t"3"
}}
"""


def patch_cs2_video_config(steam_root: str, width: int = 1080, height: int = 810) -> None:
    """Write cs2_video.txt with the given resolution (windowed, no border) to all
    Steam userdata accounts. Creates 730/local/cfg if it doesn't exist yet.
    Патчит разрешение окон для всех steamid в userdata."""
    # Try provided steam_root first, then well-known fallback paths
    candidates = []
    if steam_root:
        candidates.append(Path(steam_root))
    candidates += [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Steam"),
        Path(r"D:\Steam"),
    ]

    userdata: Path | None = None
    for root in candidates:
        ud = root / "userdata"
        if ud.exists():
            userdata = ud
            log.info("patch_cs2_video_config: using userdata at %s", ud)
            break

    if userdata is None:
        log.warning("patch_cs2_video_config: Steam userdata not found in any known path")
        return

    content = _CS2_VIDEO_TEMPLATE.format(width=width, height=height)
    patched = 0

    # Iterate over all steamid subdirectories — create 730/local/cfg if absent
    for steamid_dir in userdata.iterdir():
        if not steamid_dir.is_dir():
            continue
        cfg_dir = steamid_dir / "730" / "local" / "cfg"
        target = cfg_dir / "cs2_video.txt"
        try:
            cfg_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            log.info("patch_cs2_video_config: wrote %s", target)
            patched += 1
        except Exception as exc:
            log.warning("patch_cs2_video_config: failed to write %s — %s", target, exc)

    if patched == 0:
        log.warning("patch_cs2_video_config: no steamid dirs found under %s", userdata)
    else:
        log.info("patch_cs2_video_config: patched %d account(s) → %dx%d windowed", patched, width, height)


def patch_cs2_machine_convars(steam_root: str) -> None:
    """Copy cs2_machine_convars.vcfg from local project to all Steam userdata accounts.
    This file contains fps_max, engine_no_focus_sleep and other performance settings."""
    # Find cs2_machine_convars.vcfg - check local project folder first
    source_candidates = [
        Path(__file__).resolve().parent / "settings" / "cs2_machine_convars.vcfg",
        Path("C:/CS2_FARM/settings/cs2_machine_convars.vcfg"),
    ]
    
    source_file: Path | None = None
    for candidate in source_candidates:
        if candidate.exists():
            source_file = candidate
            log.info("patch_cs2_machine_convars: found source at %s", source_file)
            break
    
    if source_file is None:
        log.warning("patch_cs2_machine_convars: cs2_machine_convars.vcfg not found")
        return
    
    # Find Steam userdata
    candidates = []
    if steam_root:
        candidates.append(Path(steam_root))
    candidates += [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Steam"),
        Path(r"D:\Steam"),
    ]

    userdata: Path | None = None
    for root in candidates:
        ud = root / "userdata"
        if ud.exists():
            userdata = ud
            break

    if userdata is None:
        log.warning("patch_cs2_machine_convars: Steam userdata not found")
        return

    patched = 0
    # Copy to all steamid subdirectories
    for steamid_dir in userdata.iterdir():
        if not steamid_dir.is_dir():
            continue
        cfg_dir = steamid_dir / "730" / "local" / "cfg"
        target = cfg_dir / "cs2_machine_convars.vcfg"
        try:
            cfg_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
            log.info("patch_cs2_machine_convars: copied to %s", target)
            patched += 1
        except Exception as exc:
            log.warning("patch_cs2_machine_convars: failed to copy to %s — %s", target, exc)

    if patched == 0:
        log.warning("patch_cs2_machine_convars: no steamid dirs found")
    else:
        log.info("patch_cs2_machine_convars: patched %d account(s)", patched)


def launch_cs2(cs2_path: str, launch_options: str) -> subprocess.Popen:
    """Launch CS2 via 'steam.exe -applaunch 730' so Steam injects the VAC auth token.
    Launching cs2.exe directly skips token injection and triggers the VAC
    'unsigned files' error even on a clean installation."""
    opts = launch_options or FSM_CS2_LAUNCH_OPTIONS

    # Derive steam.exe path from steam_root stored in STATE
    steam_exe = ""
    if STATE.steam_root:
        candidate = Path(STATE.steam_root) / "steam.exe"
        if candidate.exists():
            steam_exe = str(candidate)

    if not steam_exe:
        # Fallback: well-known paths
        for p in [Path(r"C:\Program Files (x86)\Steam\steam.exe"),
                  Path(r"C:\Steam\steam.exe")]:
            if p.exists():
                steam_exe = str(p)
                break

    if steam_exe:
        cmd = f'"{steam_exe}" -applaunch 730 {opts}'
        log.info("Launching CS2 via Steam -applaunch: %s", steam_exe)
        return subprocess.Popen(cmd, shell=True)

    # Last resort: direct cs2.exe (no VAC token — may show unsigned-files error)
    exe = os.path.join(cs2_path, "game", "bin", "win64", "cs2.exe")
    if os.path.exists(exe):
        log.warning("steam.exe not found — launching cs2.exe directly (VAC token missing!)")
        cmd = f'"{exe}" {opts}'
        return subprocess.Popen(cmd, shell=True)

    log.error("Neither steam.exe nor cs2.exe found — cannot launch CS2")
    return subprocess.Popen(["cmd", "/c", "echo", "cs2_launch_failed"])


# ---------------------------------------------------------------------------
# CS2 UI automation — lobby open sequence
# ---------------------------------------------------------------------------

CS2_WINDOW_TITLE = "Counter-Strike 2"


def _cs2_click(ai, win_x: int, win_y: int, rel_x: int, rel_y: int) -> None:
    """Click at (rel_x, rel_y) inside CS2 window given its screen origin."""
    ai.mouse_click("left", win_x + rel_x, win_y + rel_y)


def cs2_open_lobby_ui(ai, step_delay: float = 2.5,
                       window_wait_timeout: int = 120) -> None:
    """
    Sequence to open the CS2 lobby invite window.
    Waits for the CS2 window to appear (up to window_wait_timeout seconds),
    then clicks:
      1. Escape x3
      2. "Играть"        (560,  25) relative to CS2 window
      3. "Подбор матча"  (400,  65)
      4. "Напарники"     (400, 100)
      5. "Друзья"        (1060, 25)
    step_delay seconds between each action.
    """
    log.info("=" * 60)
    log.info("cs2_open_lobby_ui: START — waiting for '%s' window (timeout %ds)...",
             CS2_WINDOW_TITLE, window_wait_timeout)

    # Wait until the CS2 window actually appears
    appeared = ai.win_wait(CS2_WINDOW_TITLE, timeout=window_wait_timeout)
    if not appeared:
        log.error("cs2_open_lobby_ui: CS2 window did NOT appear within %ds — ABORT",
                  window_wait_timeout)
        return

    log.info("cs2_open_lobby_ui: CS2 window detected — waiting %.0fs for main menu to load...",
             step_delay)
    time.sleep(step_delay)

    # Activate CS2 window and get its screen position
    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(1.0)

    wx, wy, ww, wh = ai.win_get_pos(CS2_WINDOW_TITLE)
    if wx == -1:
        log.error("cs2_open_lobby_ui: win_get_pos returned -1 — window lost after activation — ABORT")
        return
    log.info("cs2_open_lobby_ui: window screen position: x=%d y=%d  size: %dx%d", wx, wy, ww, wh)
    log.info("cs2_open_lobby_ui: READY — starting click sequence")
    log.info("=" * 60)

    # Step 1 — Escape x3
    log.info(">>> [1/5] Pressing Escape x3")
    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(0.5)
    for i in range(3):
        ai.send("{ESC}")
        log.info("    Escape %d/3 sent", i + 1)
        time.sleep(0.4)
    log.info("    Waiting %.0fs before next action...", step_delay)
    time.sleep(step_delay)

    # Step 2 — Играть (560, 25)
    log.info(">>> [2/5] Clicking 'Играть' at window-rel (560, 25) → screen (%d, %d)",
             wx + 560, wy + 25)
    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(0.5)
    _cs2_click(ai, wx, wy, 560, 25)
    log.info("    Click sent. Waiting %.0fs...", step_delay)
    time.sleep(step_delay)

    # Step 3 — Подбор матча (400, 65)
    log.info(">>> [3/5] Clicking 'Подбор матча' at window-rel (400, 65) → screen (%d, %d)",
             wx + 400, wy + 65)
    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(0.5)
    _cs2_click(ai, wx, wy, 400, 65)
    log.info("    Click sent. Waiting %.0fs...", step_delay)
    time.sleep(step_delay)

    # Step 4 — Напарники (400, 100)
    log.info(">>> [4/5] Clicking 'Напарники' at window-rel (400, 100) → screen (%d, %d)",
             wx + 400, wy + 100)
    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(0.5)
    _cs2_click(ai, wx, wy, 400, 100)
    log.info("    Click sent. Waiting %.0fs...", step_delay)
    time.sleep(step_delay)

    # Step 5 — Друзья (1060, 25): hover first, then click after 3s
    log.info(">>> [5/5] Hovering 'Друзья' at window-rel (1060, 25) → screen (%d, %d)",
             wx + 1060, wy + 25)
    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(0.5)
    ai.mouse_move(wx + 1060, wy + 25)
    log.info("    Mouse moved. Waiting 3s before click...")
    time.sleep(3)
    log.info("    Clicking 'Друзья'...")
    _cs2_click(ai, wx, wy, 1060, 25)
    log.info("    Click sent.")

    log.info("=" * 60)
    log.info("cs2_open_lobby_ui: DONE — lobby invite window should be open")


# ---------------------------------------------------------------------------
# Member sequence — get friend code (for VM 2, VM 4)
# ---------------------------------------------------------------------------

def _get_clipboard() -> str:
    """Read clipboard content via PowerShell (no extra deps)."""
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return (result.stdout or "").strip()
    except Exception as exc:
        log.warning("_get_clipboard failed: %s", exc)
    return ""


def _set_clipboard(text: str) -> bool:
    """Set clipboard content via PowerShell (base64 for safe encoding)."""
    try:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        cmd = (
            f"$b=[System.Convert]::FromBase64String('{b64}');"
            "[System.Text.Encoding]::UTF8.GetString($b)|Set-Clipboard"
        )
        result = subprocess.run(
            ["powershell", "-Command", cmd],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning("_set_clipboard failed: %s", exc)
        return False


def cs2_get_friend_code(ai, step_delay: float = 2.5,
                         window_wait_timeout: int = 120) -> str:
    """
    Member VM sequence: open friends panel and copy friend code to clipboard.
    Returns the code from clipboard, or empty string on failure.
    Steps: Escape x3, Играть, Подбор матча, Напарники, Друзья, "Ваш код".
    """
    log.info("cs2_get_friend_code: START — waiting for CS2 window (timeout %ds)...",
             window_wait_timeout)

    appeared = ai.win_wait(CS2_WINDOW_TITLE, timeout=window_wait_timeout)
    if not appeared:
        log.error("cs2_get_friend_code: CS2 window did NOT appear — ABORT")
        return ""

    log.info("cs2_get_friend_code: CS2 window detected — waiting %.0fs...", step_delay)
    time.sleep(step_delay)

    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(1.0)

    wx, wy, _, _ = ai.win_get_pos(CS2_WINDOW_TITLE)
    if wx == -1:
        log.error("cs2_get_friend_code: win_get_pos failed — ABORT")
        return ""

    # 1) Escape x3
    log.info(">>> [1/7] Escape x3")
    for i in range(3):
        ai.send("{ESC}")
        time.sleep(0.4)
    time.sleep(step_delay)

    # 2) Играть (560, 25)
    log.info(">>> [2/7] Играть (560, 25)")
    _cs2_click(ai, wx, wy, 560, 25)
    time.sleep(step_delay)

    # 3) Подбор матча (400, 65)
    log.info(">>> [3/7] Подбор матча (400, 65)")
    _cs2_click(ai, wx, wy, 400, 65)
    time.sleep(step_delay)

    # 4) Напарники (400, 100)
    log.info(">>> [4/7] Напарники (400, 100)")
    _cs2_click(ai, wx, wy, 400, 100)
    time.sleep(step_delay)

    # 5) Друзья (1060, 25): навести мышь, через 2 сек кликнуть
    log.info(">>> [5/7] Друзья (1060, 25) — наведение мыши...")
    ai.mouse_move(wx + 1060, wy + 25)
    time.sleep(2)
    _cs2_click(ai, wx, wy, 1060, 25)
    time.sleep(step_delay)

    # 6) "Ваш код" (610, 460) — copies friend code to clipboard
    log.info(">>> [6/7] Ваш код (610, 460) — copying to clipboard")
    _cs2_click(ai, wx, wy, 610, 460)
    time.sleep(1.5)

    code = _get_clipboard()

    # 7) Отмена (670, 460) — закрыть попап VM 2/4
    time.sleep(0.5)
    log.info(">>> [7/7] Отмена (670, 460)")
    _cs2_click(ai, wx, wy, 670, 460)
    if code:
        log.info("cs2_get_friend_code: код успешно скопирован: %s", code)
    else:
        log.warning("cs2_get_friend_code: буфер обмена пуст — код не получен")
    return code


# ---------------------------------------------------------------------------
# Captain sequence — invite by friend code (for VM 1, VM 3)
# Код передаётся в параметре (от контроллера), печатается посимвольно
# ---------------------------------------------------------------------------

def cs2_invite_by_code(ai, code: str, step_delay: float = 2.5,
                       window_wait_timeout: int = 120) -> bool:
    """
    Captain VM: invite player by typing friend code.
    Steps: 1-5 (Escape, Играть, Подбор матча, Напарники, Друзья),
    7-10 (input, type code, click user, Пригласить, Отмена).
    """
    log.info("cs2_invite_by_code: START — waiting for CS2 window (timeout %ds)...",
             window_wait_timeout)

    if not code:
        log.error("cs2_invite_by_code: код не передан")
        return False

    appeared = ai.win_wait(CS2_WINDOW_TITLE, timeout=window_wait_timeout)
    if not appeared:
        log.error("cs2_invite_by_code: CS2 window did NOT appear — ABORT")
        return False

    log.info("cs2_invite_by_code: код для ввода: %s", code)

    log.info("cs2_invite_by_code: waiting %.0fs...", step_delay)
    time.sleep(step_delay)

    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(1.0)

    wx, wy, _, _ = ai.win_get_pos(CS2_WINDOW_TITLE)
    if wx == -1:
        log.error("cs2_invite_by_code: win_get_pos failed — ABORT")
        return False

    # 1) Escape x3
    log.info(">>> [1/10] Escape x3")
    for i in range(3):
        ai.send("{ESC}")
        time.sleep(0.4)
    time.sleep(step_delay)

    # 2) Играть (560, 25)
    log.info(">>> [2/10] Играть (560, 25)")
    _cs2_click(ai, wx, wy, 560, 25)
    time.sleep(step_delay)

    # 3) Подбор матча (400, 65)
    log.info(">>> [3/10] Подбор матча (400, 65)")
    _cs2_click(ai, wx, wy, 400, 65)
    time.sleep(step_delay)

    # 4) Напарники (400, 100)
    log.info(">>> [4/10] Напарники (400, 100)")
    _cs2_click(ai, wx, wy, 400, 100)
    time.sleep(step_delay)

    # 5) Друзья (1060, 25): навести мышь, через 2 сек кликнуть
    log.info(">>> [5/10] Друзья (1060, 25) — наведение мыши...")
    ai.mouse_move(wx + 1060, wy + 25)
    time.sleep(2)
    _cs2_click(ai, wx, wy, 1060, 25)
    time.sleep(step_delay)

    # 7) Input "Найти игрока по коду дружбы" (400, 390) — клик, напечатать код
    log.info(">>> [7/10] Input (400, 390) — клик и ввод кода посимвольно")
    _cs2_click(ai, wx, wy, 400, 390)
    time.sleep(0.5)
    _send_text(ai, code, char_delay=0.08)
    time.sleep(2)
    log.info("    Код введён, ожидание поиска...")

    # 8) Юзер (417, 405)
    log.info(">>> [8/10] Юзер (417, 405)")
    _cs2_click(ai, wx, wy, 417, 405)
    time.sleep(step_delay)

    # 9) Пригласить (498, 492)
    log.info(">>> [9/10] Пригласить (498, 492)")
    _cs2_click(ai, wx, wy, 498, 492)
    time.sleep(step_delay)

    # 10) Отмена (670, 490)
    log.info(">>> [10/10] Отмена (670, 490)")
    _cs2_click(ai, wx, wy, 670, 490)

    log.info("cs2_invite_by_code: DONE — приглашение отправлено")
    return True


def cs2_accept_invite(ai, step_delay: float = 2.5,
                      window_wait_timeout: int = 120) -> bool:
    """
    Member VM (VM 2/4): accept lobby invite.
    Steps:
      1. Hover меню (1050, 25)
      2. Click Принятие приглашения (860, 100)
    """
    log.info("cs2_accept_invite: START — waiting for CS2 window...")
    appeared = ai.win_wait(CS2_WINDOW_TITLE, timeout=window_wait_timeout)
    if not appeared:
        log.error("cs2_accept_invite: CS2 window not found — ABORT")
        return False

    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(1.0)
    wx, wy, _, _ = ai.win_get_pos(CS2_WINDOW_TITLE)
    if wx == -1:
        log.error("cs2_accept_invite: win_get_pos failed — ABORT")
        return False

    # 1) Навести на меню (1050, 25)
    log.info(">>> [1/2] Наведение на меню (1050, 25)")
    ai.mouse_move(wx + 1050, wy + 25)
    time.sleep(2)

    # 2) Принятие приглашения (860, 100)
    log.info(">>> [2/2] Принятие приглашения (860, 100)")
    _cs2_click(ai, wx, wy, 860, 100)

    log.info("cs2_accept_invite: DONE")
    return True


def cs2_start_match(ai, window_wait_timeout: int = 120) -> bool:
    """
    Captain VM (VM 1/3): click Начать after members accepted.
    """
    log.info("cs2_start_match: START")
    appeared = ai.win_wait(CS2_WINDOW_TITLE, timeout=window_wait_timeout)
    if not appeared:
        log.error("cs2_start_match: CS2 window not found — ABORT")
        return False

    ai.win_activate(CS2_WINDOW_TITLE)
    time.sleep(1.0)
    wx, wy, _, _ = ai.win_get_pos(CS2_WINDOW_TITLE)
    if wx == -1:
        log.error("cs2_start_match: win_get_pos failed — ABORT")
        return False

    log.info(">>> Начать (900, 780)")
    _cs2_click(ai, wx, wy, 900, 780)
    log.info("cs2_start_match: DONE")
    return True


# ---------------------------------------------------------------------------
# Console log tailer — match_id detection via -condebug
# ---------------------------------------------------------------------------

MATCH_ID_RE = re.compile(r"match_id=(\d+)")
# Optional: player name, level, XP if present in console output
PLAYER_NAME_RE = re.compile(r'"([^"]+)"\s+STEAM_1:')
LEVEL_RE = re.compile(r"level[:\s=]+(\d+)", re.I)
XP_RE = re.compile(r"xp[:\s=]+(\d+)", re.I)
# From "Received Steam datagram ticket for server steamid:90282794870437889"
SERVER_STEAMID_RE = re.compile(r"steamid:(\d+)", re.I)


def _parse_console_line(line: str) -> dict:
    """Extract match_id and any player/level/xp info from a console line."""
    extra: dict = {}
    m = MATCH_ID_RE.search(line)
    if m:
        extra["match_id"] = m.group(1)
    m = SERVER_STEAMID_RE.search(line)
    if m:
        extra["server_steamid"] = m.group(1)
    m = PLAYER_NAME_RE.search(line)
    if m:
        extra.setdefault("players", []).append(m.group(1))
    m = LEVEL_RE.search(line)
    if m:
        extra["level"] = m.group(1)
    m = XP_RE.search(line)
    if m:
        extra["xp"] = m.group(1)
    return extra


def _run_console_tail(cs2_path: str, timeout_sec: int = 120) -> None:
    """Tail console.log and update STATE when match_id found."""
    console_path = Path(cs2_path) / "game" / "csgo" / "console.log"
    if not console_path.exists():
        log.warning("console.log not found: %s", console_path)
        STATE.update(match_id="", match_found_at="", console_extra={})
        return

    STATE.update(match_id="", match_found_at="", console_extra={})
    STATE._tail_stop.clear()
    deadline = time.time() + timeout_sec
    log.info("Console tail: watching %s (timeout %ds)", console_path, timeout_sec)

    try:
        with open(console_path, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            while not STATE._tail_stop.is_set() and time.time() < deadline:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                extra = _parse_console_line(line)
                if "match_id" in extra:
                    from datetime import datetime
                    STATE.update(
                        match_id=extra["match_id"],
                        match_found_at=datetime.now().isoformat(),
                        console_extra={k: v for k, v in extra.items() if k != "match_id"},
                    )
                    log.info("Console tail: match_id=%s", extra["match_id"])
                    return
    except Exception as exc:
        log.exception("Console tail error: %s", exc)
    finally:
        log.info("Console tail: stopped")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length) if length else b"{}"
    return json.loads(body) if body else {}


def _respond(handler: BaseHTTPRequestHandler, data: dict, code: int = 200) -> None:
    body = json.dumps(data).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class VMAgentHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        log.debug("HTTP %s %s", self.command, self.path)

    def do_GET(self) -> None:
        if self.path == "/status":
            _respond(self, STATE.snapshot())
        else:
            _respond(self, {"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            data = _read_json(self)
        except Exception as exc:
            _respond(self, {"error": f"bad json: {exc}"}, 400)
            return

        routes = {
            "/login":               self._handle_login,
            "/logout":              self._handle_logout,
            "/cs2/launch":          self._handle_cs2_launch,
            "/cs2/kill":            self._handle_cs2_kill,
            "/cs2/win_info":        self._handle_cs2_win_info,
            "/lobby/create":        self._handle_lobby_create,
            "/lobby/accept":        self._handle_lobby_accept,
            "/lobby/create_ui":     self._handle_lobby_create_ui,
            "/lobby/get_friend_code": self._handle_lobby_get_friend_code,
            "/lobby/set_clipboard": self._handle_lobby_set_clipboard,
            "/lobby/invite_by_code": self._handle_lobby_invite_by_code,
            "/lobby/accept_invite":  self._handle_lobby_accept_invite,
            "/lobby/start_match":    self._handle_lobby_start_match,
            "/cs2/start_console_tail": self._handle_cs2_start_console_tail,
            "/cs2/stop_console_tail":  self._handle_cs2_stop_console_tail,
        }
        fn = routes.get(self.path)
        if fn:
            fn(data)
        else:
            _respond(self, {"error": "unknown endpoint"}, 404)

    # ------------------------------------------------------------------
    def _handle_login(self, data: dict) -> None:
        login          = data.get("login", "")
        password       = data.get("password", "")
        shared_secret  = data.get("shared_secret", "")
        steam_path     = data.get("steam_path", r"C:\Program Files (x86)\Steam\steam.exe")
        extra_opts     = data.get("steam_launch_options", "")

        if not login or not password:
            _respond(self, {"error": "login and password required"}, 400)
            return
        if STATE.state not in ("idle", "error"):
            _respond(self, {"error": f"busy: {STATE.state}"}, 409)
            return

        STATE.update(state="logging_in", login=login, steamid="", error="",
                     steam_root=str(Path(steam_path).parent))

        def _do() -> None:
            try:
                ok = steam_login_fsm(login, password, shared_secret, steam_path, extra_opts)
                if ok:
                    STATE.update(state="logged_in")
                else:
                    STATE.update(state="error", error="Steam login failed after all attempts")
            except Exception as exc:
                STATE.update(state="error", error=str(exc))
                log.exception("Login thread error")

        threading.Thread(target=_do, daemon=True).start()
        _respond(self, {"ok": True, "state": "logging_in"})

    # ------------------------------------------------------------------
    def _handle_logout(self, data: dict) -> None:
        kill_steam()
        STATE.update(state="idle", steamid="", login="", error="")
        _respond(self, {"ok": True})

    # ------------------------------------------------------------------
    def _handle_cs2_launch(self, data: dict) -> None:
        if STATE.state != "logged_in":
            _respond(self, {"error": f"need logged_in, have: {STATE.state}"}, 409)
            return

        cs2_path     = data.get("cs2_path", "")
        launch_opts  = data.get("launch_options", "")
        # If True — automatically run the lobby UI click sequence after CS2 window appears
        open_lobby   = bool(data.get("open_lobby_ui", False))
        step_delay   = float(data.get("step_delay", 2.5))

        if not cs2_path:
            _respond(self, {"error": "cs2_path required"}, 400)
            return

        STATE.update(state="launching_cs2", cs2_path=cs2_path)

        def _do() -> None:
            try:
                # Patch CS2 config files BEFORE launch
                patch_cs2_video_config(STATE.steam_root)
                patch_cs2_machine_convars(STATE.steam_root)
                opts_with_res = _inject_resolution_args(launch_opts)

                log.info("CS2: sending launch command...")
                proc = launch_cs2(cs2_path, opts_with_res)
                STATE._cs2_proc = proc

                # Wait 6 seconds for the Steam launcher handoff, then mark process as started.
                # The game window itself may take 30-90 seconds more to fully appear.
                time.sleep(6)
                STATE.update(state="cs2_running")
                log.info("CS2: process launched (state=cs2_running). "
                         "Game window may still be loading...")

                if open_lobby:
                    ai = get_autoit()
                    if ai is None:
                        log.error("CS2 launch: open_lobby_ui=True but AutoIt not available")
                        return
                    log.info("CS2: open_lobby_ui=True — starting lobby UI sequence")
                    STATE.update(state="opening_lobby_ui")
                    try:
                        cs2_open_lobby_ui(ai, step_delay=step_delay)
                        STATE.update(state="lobby_ui_open")
                    except Exception as exc:
                        STATE.update(state="error", error=str(exc))
                        log.exception("CS2 open_lobby_ui error")
            except Exception as exc:
                STATE.update(state="error", error=str(exc))
                log.exception("CS2 launch error")

        threading.Thread(target=_do, daemon=True).start()
        _respond(self, {"ok": True, "state": "launching_cs2"})

    # ------------------------------------------------------------------
    def _handle_cs2_kill(self, data: dict) -> None:
        proc = STATE._cs2_proc
        if proc and proc.poll() is None:
            proc.terminate()
        subprocess.run(["taskkill", "/F", "/IM", "cs2.exe"], capture_output=True)
        STATE._cs2_proc = None
        if STATE.state in ("cs2_running", "in_lobby", "launching_cs2"):
            STATE.update(state="logged_in")
        _respond(self, {"ok": True})

    # ------------------------------------------------------------------
    def _handle_cs2_win_info(self, data: dict) -> None:
        """Diagnostic: check if AutoIt can find the CS2 window and return its position."""
        ai = get_autoit()
        if ai is None:
            _respond(self, {"error": "AutoIt not available"}, 500)
            return

        exists = ai.win_exists(CS2_WINDOW_TITLE)
        log.info("win_info: win_exists('%s') = %s", CS2_WINDOW_TITLE, exists)

        result: dict = {
            "window_title": CS2_WINDOW_TITLE,
            "win_exists": bool(exists),
            "win_pos": None,
        }

        if exists:
            wx, wy, ww, wh = ai.win_get_pos(CS2_WINDOW_TITLE)
            result["win_pos"] = {"x": wx, "y": wy, "w": ww, "h": wh}
            log.info("win_info: position x=%d y=%d  size=%dx%d", wx, wy, ww, wh)
        else:
            log.warning("win_info: CS2 window NOT found — check window title or cs2.exe running")

        _respond(self, result)

    # ------------------------------------------------------------------
    def _handle_lobby_create(self, data: dict) -> None:
        """Captain VM: run cs2_lobby.js to invite members."""
        steamids      = data.get("steamids", [])
        shared_secret = data.get("shared_secret", "")
        password      = data.get("password", "")

        if not steamids:
            _respond(self, {"error": "steamids required"}, 400)
            return

        script = _find_script("cs2_lobby.js")
        cmd = ["node", script, STATE.login, password,
               shared_secret, "CS2Farm_captain", "captain", *steamids]
        log.info("Starting lobby captain: %s", " ".join(cmd[:6]))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8")

        def _reader() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    log.info("lobby event: %s", msg)
                    if msg.get("event") == "logged_in":
                        STATE.update(state="in_lobby")
                except Exception:
                    log.debug("lobby stdout: %s", line)

        threading.Thread(target=_reader, daemon=True).start()
        _respond(self, {"ok": True})

    # ------------------------------------------------------------------
    def _handle_lobby_accept(self, data: dict) -> None:
        """Member VM: write connect_lobby cfg for CS2 to pick up."""
        lobby_id     = data.get("lobby_id", "")
        cs2_cfg_path = data.get("cs2_cfg_path", "")

        if cs2_cfg_path and lobby_id:
            cfg_file = os.path.join(cs2_cfg_path, "cfg", "cs2farm_lobby.cfg")
            try:
                os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
                with open(cfg_file, "w") as f:
                    f.write(f"connect_lobby {lobby_id}\n")
                log.info("Lobby cfg written: %s", cfg_file)
            except Exception as exc:
                _respond(self, {"error": str(exc)}, 500)
                return

        STATE.update(state="in_lobby")
        _respond(self, {"ok": True})

    # ------------------------------------------------------------------
    def _handle_lobby_create_ui(self, data: dict) -> None:
        """Captain VM: open CS2 lobby invite UI via AutoIt mouse clicks.
        Runs asynchronously; returns immediately with state 'opening_lobby_ui'.
        Optional params:
          initial_wait  — seconds to wait after CS2 launch before clicking (default 20)
          step_delay    — seconds between each UI action (default 2.5)
        """
        if STATE.state not in ("cs2_running", "in_lobby"):
            _respond(self, {"error": f"need cs2_running, have: {STATE.state}"}, 409)
            return

        ai = get_autoit()
        if ai is None:
            _respond(self, {"error": "AutoIt not available"}, 500)
            return

        step_delay          = float(data.get("step_delay", 2.5))
        window_wait_timeout = int(data.get("window_wait_timeout", 120))

        STATE.update(state="opening_lobby_ui")

        def _do() -> None:
            try:
                cs2_open_lobby_ui(ai, step_delay=step_delay,
                                  window_wait_timeout=window_wait_timeout)
                STATE.update(state="lobby_ui_open")
            except Exception as exc:
                STATE.update(state="error", error=str(exc))
                log.exception("lobby_create_ui error")

        threading.Thread(target=_do, daemon=True).start()
        _respond(self, {"ok": True, "state": "opening_lobby_ui"})

    # ------------------------------------------------------------------
    def _handle_lobby_get_friend_code(self, data: dict) -> None:
        """Member VM: run UI sequence to copy friend code, return it."""
        if STATE.state not in ("idle", "logged_in", "cs2_running", "in_lobby"):
            _respond(self, {"error": f"busy: {STATE.state}"}, 409)
            return

        ai = get_autoit()
        if ai is None:
            _respond(self, {"error": "AutoIt not available"}, 500)
            return

        step_delay = float(data.get("step_delay", 2.5))
        window_wait_timeout = int(data.get("window_wait_timeout", 120))

        try:
            code = cs2_get_friend_code(ai, step_delay=step_delay,
                                       window_wait_timeout=window_wait_timeout)
            _respond(self, {"ok": True, "friend_code": code})
        except Exception as exc:
            log.exception("lobby_get_friend_code error")
            _respond(self, {"error": str(exc)}, 500)

    # ------------------------------------------------------------------
    def _handle_lobby_set_clipboard(self, data: dict) -> None:
        """Captain VM: set clipboard to received friend code."""
        code = data.get("code", "")
        ok = _set_clipboard(code)
        if ok:
            log.info("Clipboard set with friend code: %s", code)
        _respond(self, {"ok": ok, "code": code})

    # ------------------------------------------------------------------
    def _handle_lobby_invite_by_code(self, data: dict) -> None:
        """Captain VM: run invite sequence, typing friend code from request."""
        if STATE.state not in ("idle", "logged_in", "cs2_running", "in_lobby"):
            _respond(self, {"error": f"busy: {STATE.state}"}, 409)
            return

        code = data.get("friend_code", "").strip()
        if not code:
            _respond(self, {"error": "friend_code required"}, 400)
            return

        ai = get_autoit()
        if ai is None:
            _respond(self, {"error": "AutoIt not available"}, 500)
            return

        step_delay = float(data.get("step_delay", 2.5))
        window_wait_timeout = int(data.get("window_wait_timeout", 120))

        try:
            ok = cs2_invite_by_code(ai, code, step_delay=step_delay,
                                    window_wait_timeout=window_wait_timeout)
            _respond(self, {"ok": ok})
        except Exception as exc:
            log.exception("lobby_invite_by_code error")
            _respond(self, {"error": str(exc)}, 500)

    # ------------------------------------------------------------------
    def _handle_lobby_accept_invite(self, data: dict) -> None:
        """Member VM (VM 2/4): hover menu, click accept invite."""
        ai = get_autoit()
        if ai is None:
            _respond(self, {"error": "AutoIt not available"}, 500)
            return
        step_delay = float(data.get("step_delay", 2.5))
        window_wait_timeout = int(data.get("window_wait_timeout", 120))
        try:
            ok = cs2_accept_invite(ai, step_delay=step_delay,
                                   window_wait_timeout=window_wait_timeout)
            _respond(self, {"ok": ok})
        except Exception as exc:
            log.exception("lobby_accept_invite error")
            _respond(self, {"error": str(exc)}, 500)

    # ------------------------------------------------------------------
    def _handle_lobby_start_match(self, data: dict) -> None:
        """Captain VM (VM 1/3): click Начать."""
        ai = get_autoit()
        if ai is None:
            _respond(self, {"error": "AutoIt not available"}, 500)
            return
        window_wait_timeout = int(data.get("window_wait_timeout", 120))
        try:
            ok = cs2_start_match(ai, window_wait_timeout=window_wait_timeout)
            _respond(self, {"ok": ok})
        except Exception as exc:
            log.exception("lobby_start_match error")
            _respond(self, {"error": str(exc)}, 500)

    # ------------------------------------------------------------------
    def _handle_cs2_start_console_tail(self, data: dict) -> None:
        """Start tailing console.log for match_id. Uses STATE.cs2_path or data."""
        STATE._tail_stop.set()
        time.sleep(0.3)
        cs2_path = data.get("cs2_path", "") or getattr(STATE, "cs2_path", "")
        if not cs2_path:
            _respond(self, {"error": "cs2_path required (or launch CS2 first)"}, 400)
            return
        timeout = int(data.get("timeout_sec", 120))
        threading.Thread(
            target=_run_console_tail,
            args=(cs2_path, timeout),
            daemon=True,
        ).start()
        _respond(self, {"ok": True, "message": "Console tail started"})

    def _handle_cs2_stop_console_tail(self, data: dict) -> None:
        """Stop the console tail thread."""
        STATE._tail_stop.set()
        _respond(self, {"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("VM Agent v3.0 — port %d", PORT)
    ai = get_autoit()
    if ai:
        log.info("AutoIt ready — Steam GUI login enabled")
    else:
        log.error("AutoIt не найден — см. папку autoit/lib/ рядом с vm_agent.py")

    server = HTTPServer(("0.0.0.0", PORT), VMAgentHandler)
    log.info("Listening on 0.0.0.0:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutdown")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
