"""
HTTP-клиент и оркестрация VM без GUI (общий код для cs2_farm_controller и панели).
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOGPASS_PATH = ROOT / "logpass.txt"
MAFILES_DIR = ROOT / "maFiles"

_log = logging.getLogger("farm_vm_client")

STATE_COLORS = {
    "idle": "#555555",
    "logging_in": "#c8a200",
    "logged_in": "#2d8a4e",
    "launching_cs2": "#c8a200",
    "cs2_running": "#1e6fc1",
    "opening_lobby_ui": "#8e44ad",
    "lobby_ui_open": "#8e44ad",
    "in_lobby": "#1e6fc1",
    "error": "#c0392b",
    "unreachable": "#7f8c8d",
}

STATE_LABELS = {
    "idle": "Ожидание",
    "logging_in": "Вход в Steam...",
    "logged_in": "Steam: вошли",
    "launching_cs2": "Запуск CS2...",
    "cs2_running": "CS2 запущен",
    "opening_lobby_ui": "Открытие лобби...",
    "lobby_ui_open": "Лобби открыто",
    "in_lobby": "В лобби",
    "error": "Ошибка",
    "unreachable": "Недоступна",
}


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_accounts() -> list[dict]:
    """Аккаунты из logpass.txt и maFiles в каталоге с config.json."""
    accounts: list[dict] = []
    ma_index: dict[str, dict] = {}
    if MAFILES_DIR.exists():
        for mf in MAFILES_DIR.glob("*.maFile"):
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                acc_name = data.get("account_name", "").lower()
                if acc_name:
                    ma_index[acc_name] = data
            except Exception:
                pass

    if LOGPASS_PATH.exists():
        for line in LOGPASS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            login, _, password = line.partition(":")
            login = login.strip()
            password = password.strip()
            ma = ma_index.get(login.lower(), {})
            accounts.append(
                {
                    "login": login,
                    "password": password,
                    "shared_secret": ma.get("shared_secret", ""),
                    "identity_secret": ma.get("identity_secret", ""),
                    "has_2fa": bool(ma.get("shared_secret")),
                }
            )

    return accounts


REQUEST_TIMEOUT = 10


def vm_url(vm: dict, path: str) -> str:
    return f"http://{vm['ip']}:{vm['port']}{path}"


def vm_status(vm: dict) -> dict:
    try:
        r = requests.get(vm_url(vm, "/status"), timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"state": "unreachable", "error": str(exc)}


def vm_login(vm: dict, account: dict, cfg: dict) -> dict:
    steam_path = vm.get("steam_path") or cfg.get("steam_path", "")
    payload = {
        "login": account["login"],
        "password": account["password"],
        "shared_secret": account["shared_secret"],
        "machine_name": vm["name"].replace(" ", "_"),
        "steam_path": steam_path,
        "steam_launch_options": cfg.get("steam_launch_options", ""),
    }
    try:
        r = requests.post(vm_url(vm, "/login"), json=payload, timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_launch_cs2(vm: dict, cfg: dict) -> dict:
    cs2_path = vm.get("cs2_path") or cfg.get("cs2_path", "")
    payload = {
        "cs2_path": cs2_path,
        "launch_options": cfg.get("cs2_launch_options", ""),
    }
    try:
        r = requests.post(vm_url(vm, "/cs2/launch"), json=payload, timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_logout(vm: dict) -> dict:
    try:
        r = requests.post(vm_url(vm, "/logout"), json={}, timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_lobby_ui(vm: dict, step_delay: float = 2.5) -> dict:
    payload = {"step_delay": step_delay, "window_wait_timeout": 30}
    try:
        r = requests.post(vm_url(vm, "/lobby/create_ui"), json=payload, timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_get_friend_code(vm: dict, step_delay: float = 2.5) -> dict:
    payload = {"step_delay": step_delay, "window_wait_timeout": 120}
    try:
        r = requests.post(
            vm_url(vm, "/lobby/get_friend_code"),
            json=payload,
            timeout=180,
        )
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_set_clipboard(vm: dict, code: str) -> dict:
    try:
        r = requests.post(
            vm_url(vm, "/lobby/set_clipboard"),
            json={"code": code},
            timeout=REQUEST_TIMEOUT,
        )
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_invite_by_code(vm: dict, friend_code: str, step_delay: float = 2.5) -> dict:
    payload = {
        "friend_code": friend_code,
        "step_delay": step_delay,
        "window_wait_timeout": 120,
    }
    try:
        r = requests.post(
            vm_url(vm, "/lobby/invite_by_code"),
            json=payload,
            timeout=180,
        )
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_accept_invite(vm: dict, step_delay: float = 2.5) -> dict:
    payload = {"step_delay": step_delay, "window_wait_timeout": 120}
    try:
        r = requests.post(
            vm_url(vm, "/lobby/accept_invite"),
            json=payload,
            timeout=60,
        )
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_start_match(vm: dict) -> dict:
    try:
        r = requests.post(vm_url(vm, "/lobby/start_match"), json={}, timeout=60)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_start_console_tail(vm: dict, cfg: dict, timeout_sec: int = 120) -> dict:
    payload = {"cs2_path": cfg.get("cs2_path", ""), "timeout_sec": timeout_sec}
    try:
        r = requests.post(
            vm_url(vm, "/cs2/start_console_tail"),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_win_info(vm: dict) -> dict:
    try:
        r = requests.post(vm_url(vm, "/cs2/win_info"), json={}, timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def vm_lobby_create(vm: dict, account: dict, member_steamids: list[str]) -> dict:
    payload = {
        "password": account["password"],
        "shared_secret": account["shared_secret"],
        "steamids": member_steamids,
    }
    try:
        r = requests.post(vm_url(vm, "/lobby/create"), json=payload, timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def wait_for_state(vm: dict, target_state: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = vm_status(vm)
        if status.get("state") == target_state:
            return True
        if status.get("state") == "error":
            _log.error("VM %s error: %s", vm.get("id"), status.get("error"))
            return False
        time.sleep(3)
    return False


def run_single_vm(
    vm: dict,
    account: dict,
    cfg: dict,
    on_log: Any = None,
) -> None:
    def log_msg(msg: str) -> None:
        _log.info(msg)
        if on_log:
            on_log(msg)

    log_msg(f"=== One Start: {vm['name']} / {account['login']} ===")

    if not account.get("login"):
        log_msg(f"ERROR: No account selected for {vm['name']}")
        return

    log_msg(f"[{vm['name']}] Resetting VM (logout)...")
    vm_logout(vm)
    time.sleep(3)

    log_msg(f"[{vm['name']}] Sending login for {account['login']}...")
    resp = vm_login(vm, account, cfg)
    if "error" in resp:
        log_msg(f"[{vm['name']}] Login request failed: {resp['error']}")
        return

    success = wait_for_state(vm, "logged_in", timeout=cfg.get("login_timeout_sec", 90))
    if not success:
        log_msg(f"[{vm['name']}] Login timeout — aborting")
        return

    log_msg(f"[{vm['name']}] Logged in. Launching CS2...")
    resp = vm_launch_cs2(vm, cfg)
    if "error" in resp:
        log_msg(f"[{vm['name']}] CS2 launch error: {resp['error']}")
        return

    log_msg(f"[{vm['name']}] CS2 launch command sent. Done.")


def run_2v2_farm(
    assignments: list[tuple[dict, dict]],
    cfg: dict,
    on_log: Any = None,
) -> None:
    """
    assignments: list of (vm, account) — exactly 4 entries
    cfg: loaded config.json
    on_log: callable(message: str) for GUI log updates
    """

    def log_msg(msg: str) -> None:
        _log.info(msg)
        if on_log:
            on_log(msg)

    if len(assignments) != 4:
        log_msg("ERROR: Exactly 4 VM-account assignments required for 2v2")
        return

    log_msg("=== CS2 Farm 2v2 Start ===")

    log_msg("Step 1: Logging in to Steam on VMs sequentially (1 → 2 → 3 → 4)...")

    for idx, (vm, account) in enumerate(assignments, start=1):
        log_msg(f"  [{idx}/{len(assignments)}] [{vm['name']}] Sending login for {account['login']}...")
        resp = vm_login(vm, account, cfg)
        if "error" in resp:
            log_msg(f"  [{vm['name']}] Login request failed: {resp['error']}")
            log_msg(f"ERROR: Login failed on [{vm['name']}]. Aborting.")
            return
        success = wait_for_state(vm, "logged_in", timeout=cfg.get("login_timeout_sec", 90))
        if success:
            log_msg(f"  [{vm['name']}] ✓ Logged in as {account['login']}")
        else:
            log_msg(f"  [{vm['name']}] Login timeout for {account['login']}. Aborting.")
            return

    log_msg("All VMs logged in successfully! Proceeding to CS2 launch...")

    log_msg("Step 2: Launching CS2 on all VMs...")

    def do_launch(vm: dict) -> tuple[str, bool]:
        resp = vm_launch_cs2(vm, cfg)
        vname = vm.get("name", "?")
        if "error" in resp:
            log_msg(f"  [{vname}] CS2 launch error: {resp['error']}")
            return str(vm.get("id") or vname), False
        success = wait_for_state(vm, "cs2_running", timeout=120)
        if success:
            log_msg(f"  [{vname}] CS2 running")
        else:
            log_msg(f"  [{vname}] CS2 launch timeout")
        return str(vm.get("id") or vname), success

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(do_launch, vm): vm for vm, _ in assignments}
        for f in as_completed(futures):
            f.result()

    log_msg("Step 3: Collecting SteamIDs from member VMs...")
    captain_vm, captain_acc = assignments[0]
    member_steamids: list[str] = []
    for vm, _ in assignments[1:]:
        status = vm_status(vm)
        sid = status.get("steamid", "")
        if sid:
            member_steamids.append(sid)
            log_msg(f"  [{vm['name']}] SteamID: {sid}")
        else:
            log_msg(f"  [{vm['name']}] WARNING: No SteamID retrieved")

    log_msg(f"Step 4: Captain [{captain_vm['name']}] creating lobby and inviting members...")
    resp = vm_lobby_create(captain_vm, captain_acc, member_steamids)
    if "error" in resp:
        log_msg(f"Lobby creation error: {resp['error']}")
    else:
        log_msg("Lobby created! Invites sent to members.")

    log_msg("=== Farm session started. Monitor VM status in the table. ===")
