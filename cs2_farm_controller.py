"""
cs2_farm_controller.py — CS2 Farm 2v2 Controller
Main GUI application that runs on the host PC and coordinates 4 VMs.

Flow:
  1. Load accounts from logpass.txt + maFiles (каталог со скриптом)
  2. User selects 4 accounts → assigns to 4 VMs (drag-drop or dropdowns)
  3. Press "Start" → parallel Steam login on all 4 VMs
  4. After all logged in → captain (VM1) creates lobby, invites VM2-4
  5. Members accept → CS2 launched → match starts
  6. Status panel shows live state of each VM (polling every 5 s)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from farm_vm_client import (
    CONFIG_PATH,
    LOGPASS_PATH,
    MAFILES_DIR,
    ROOT as PROJECT_DIR,
    STATE_COLORS,
    STATE_LABELS,
    load_accounts,
    load_config,
    run_2v2_farm,
    run_single_vm,
    vm_accept_invite,
    vm_get_friend_code,
    vm_invite_by_code,
    vm_lobby_ui,
    vm_logout,
    vm_set_clipboard,
    vm_start_console_tail,
    vm_start_match,
    vm_status,
    vm_win_info,
)

# ---------------------------------------------------------------------------
# GUI — CustomTkinter
# ---------------------------------------------------------------------------
try:
    import customtkinter as ctk  # type: ignore
except ImportError:
    raise SystemExit("customtkinter not found. Run: python -m pip install customtkinter")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = PROJECT_DIR / "controller.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("controller")


class VMRow:
    """One row in the VM status table."""

    def __init__(self, parent: ctk.CTkFrame, vm: dict, accounts: list[dict],
                 row: int, on_single_start: Any = None,
                 on_lobby_ui: Any = None, on_win_info: Any = None) -> None:
        self.vm = vm
        self._account_map = {a["login"]: a for a in accounts}

        ctk.CTkLabel(parent, text=vm["name"], font=("Roboto", 13, "bold"),
                     anchor="w", width=80).grid(row=row, column=0, padx=(8, 4), pady=4, sticky="w")

        ctk.CTkLabel(parent, text=f"{vm['ip']}:{vm['port']}",
                     font=("Roboto", 11), text_color="#aaaaaa",
                     anchor="w", width=160).grid(row=row, column=1, padx=4, pady=4, sticky="w")

        logins = [a["login"] for a in accounts]
        self.account_var = ctk.StringVar(value=logins[row] if row < len(logins) else (logins[0] if logins else ""))
        self.account_menu = ctk.CTkOptionMenu(parent, values=logins if logins else ["—"],
                                               variable=self.account_var, width=180,
                                               font=("Roboto", 12))
        self.account_menu.grid(row=row, column=2, padx=4, pady=4)

        self.fa_label = ctk.CTkLabel(parent, text="", font=("Roboto", 11), width=40)
        self.fa_label.grid(row=row, column=3, padx=4, pady=4)
        self._update_fa_indicator()

        self.status_label = ctk.CTkLabel(parent, text=STATE_LABELS["idle"],
                                          font=("Roboto", 12), width=140,
                                          corner_radius=6, fg_color=STATE_COLORS["idle"],
                                          text_color="white")
        self.status_label.grid(row=row, column=4, padx=4, pady=4)

        self.steamid_label = ctk.CTkLabel(parent, text="—", font=("Roboto", 10),
                                           text_color="#aaaaaa", width=160, anchor="w")
        self.steamid_label.grid(row=row, column=5, padx=(4, 4), pady=4, sticky="w")

        self.btn_one = ctk.CTkButton(
            parent, text="One Start",
            font=("Roboto", 11, "bold"),
            fg_color="#2d6a2d", hover_color="#1f4f1f",
            width=90, height=28,
            command=lambda: on_single_start(self) if on_single_start else None,
        )
        self.btn_one.grid(row=row, column=6, padx=(4, 4), pady=4)

        self.btn_lobby_ui = ctk.CTkButton(
            parent, text="Лобби UI",
            font=("Roboto", 11, "bold"),
            fg_color="#7b2d8a", hover_color="#5a1f66",
            width=80, height=28,
            command=lambda: on_lobby_ui(self) if on_lobby_ui else None,
        )
        self.btn_lobby_ui.grid(row=row, column=7, padx=(4, 4), pady=4)

        self.btn_win_info = ctk.CTkButton(
            parent, text="Win?",
            font=("Roboto", 10),
            fg_color="#333333", hover_color="#444444",
            width=46, height=28,
            command=lambda: on_win_info(self) if on_win_info else None,
        )
        self.btn_win_info.grid(row=row, column=8, padx=(4, 8), pady=4)

        self.account_var.trace_add("write", lambda *_: self._update_fa_indicator())

    def _update_fa_indicator(self) -> None:
        acc = self._account_map.get(self.account_var.get(), {})
        if acc.get("has_2fa"):
            self.fa_label.configure(text="2FA", text_color="#27ae60")
        else:
            self.fa_label.configure(text="no2FA", text_color="#e74c3c")

    def selected_account(self) -> dict:
        return self._account_map.get(self.account_var.get(), {})

    def update_status(self, state: str, steamid: str = "", error: str = "") -> None:
        color = STATE_COLORS.get(state, "#555555")
        label = STATE_LABELS.get(state, state)
        if state == "error" and error:
            label = f"Ошибка: {error[:30]}"
        self.status_label.configure(text=label, fg_color=color)
        self.steamid_label.configure(text=steamid if steamid else "—")


class CS2FarmApp(ctk.CTk):
    def __init__(self, cfg: dict, accounts: list[dict]) -> None:
        super().__init__()
        self.cfg = cfg
        self.accounts = accounts
        self._polling = False
        self._farm_thread: threading.Thread | None = None
        self._last_confirmed_match_id: str = ""

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("CS2 Farm 2v2 — Controller")
        self.geometry("900x620")
        self.resizable(True, True)

        self._build_ui()
        self._start_polling()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, corner_radius=0, fg_color="#1a1a2e")
        header.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        ctk.CTkLabel(header, text="CS2 Farm 2v2", font=("Roboto", 20, "bold"),
                     text_color="#4fc3f7").pack(side="left", padx=16, pady=12)
        ctk.CTkLabel(header, text=f"{len(self.accounts)} аккаунтов загружено",
                     font=("Roboto", 12), text_color="#aaaaaa").pack(side="left", padx=8)

        table_frame = ctk.CTkFrame(self, corner_radius=8)
        table_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(12, 4))
        table_frame.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6), weight=1)

        headers = ["VM", "IP:Port", "Аккаунт", "2FA", "Статус", "SteamID", "", "", ""]
        for col, h in enumerate(headers):
            ctk.CTkLabel(table_frame, text=h, font=("Roboto", 11, "bold"),
                          text_color="#888888").grid(row=0, column=col, padx=4, pady=(8, 2))

        self.vm_rows: list[VMRow] = []
        for i, vm in enumerate(self.cfg["vms"]):
            row = VMRow(table_frame, vm, self.accounts, i + 1,
                        on_single_start=self._on_single_start,
                        on_lobby_ui=self._on_lobby_ui,
                        on_win_info=self._on_win_info)
            self.vm_rows.append(row)

        ctrl = ctk.CTkFrame(self, corner_radius=8)
        ctrl.grid(row=2, column=0, sticky="ew", padx=12, pady=4)

        self.btn_start = ctk.CTkButton(ctrl, text="Запустить 2v2",
                                        font=("Roboto", 14, "bold"),
                                        fg_color="#1e6fc1", hover_color="#1557a0",
                                        width=180, height=40,
                                        command=self._on_start)
        self.btn_start.pack(side="left", padx=12, pady=10)

        self.btn_start_lobby = ctk.CTkButton(ctrl, text="Старт Лобби",
                                              font=("Roboto", 13, "bold"),
                                              fg_color="#8e44ad", hover_color="#6c3483",
                                              width=140, height=40,
                                              command=self._on_start_lobby)
        self.btn_start_lobby.pack(side="left", padx=4, pady=10)

        self.btn_stop = ctk.CTkButton(ctrl, text="Стоп (выход из Steam)",
                                       font=("Roboto", 13),
                                       fg_color="#7f1717", hover_color="#5c1010",
                                       width=200, height=40,
                                       command=self._on_stop)
        self.btn_stop.pack(side="left", padx=4, pady=10)

        self.btn_refresh = ctk.CTkButton(ctrl, text="Обновить статус",
                                          font=("Roboto", 12),
                                          fg_color="#333333", hover_color="#444444",
                                          width=150, height=40,
                                          command=self._manual_refresh)
        self.btn_refresh.pack(side="left", padx=4, pady=10)

        ctk.CTkLabel(ctrl, text="Капитаны: VM1, VM3  |  Участники: VM2, VM4  |  Нажмите Старт Лобби после загрузки CS2",
                      font=("Roboto", 11), text_color="#aaaaaa").pack(side="right", padx=16)

        log_frame = ctk.CTkFrame(self, corner_radius=8)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=12, pady=(4, 12))
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(log_frame, text="Лог", font=("Roboto", 11, "bold"),
                      anchor="w").grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))

        self.log_box = ctk.CTkTextbox(log_frame, font=("Consolas", 11),
                                       state="disabled", wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=(2, 8))

    def _append_log(self, msg: str) -> None:
        def _do() -> None:
            self.log_box.configure(state="normal")
            ts = time.strftime("%H:%M:%S")
            self.log_box.insert("end", f"[{ts}] {msg}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _do)

    def _on_start(self) -> None:
        if self._farm_thread and self._farm_thread.is_alive():
            self._append_log("Уже запущено!")
            return

        assignments = [(row.vm, row.selected_account()) for row in self.vm_rows]

        for vm, acc in assignments:
            if not acc.get("login"):
                self._append_log(f"Ошибка: для {vm['name']} не выбран аккаунт")
                return
            if not acc.get("shared_secret"):
                self._append_log(f"Предупреждение: {acc['login']} не имеет 2FA (maFile отсутствует)")

        self.btn_start.configure(state="disabled")
        self._append_log("Запускаем 2v2 ферму...")

        self._farm_thread = threading.Thread(
            target=run_2v2_farm,
            args=(assignments, self.cfg, self._append_log),
            daemon=True,
        )
        self._farm_thread.start()

        def _wait_and_enable() -> None:
            if self._farm_thread:
                self._farm_thread.join()
            self.after(0, lambda: self.btn_start.configure(state="normal"))

        threading.Thread(target=_wait_and_enable, daemon=True).start()

    def _on_single_start(self, vm_row: "VMRow") -> None:
        acc = vm_row.selected_account()
        if not acc.get("login"):
            self._append_log(f"Ошибка: для {vm_row.vm['name']} не выбран аккаунт")
            return

        vm_row.btn_one.configure(state="disabled")
        self._append_log(f"One Start: {vm_row.vm['name']} / {acc['login']}")

        def _run() -> None:
            run_single_vm(vm_row.vm, acc, self.cfg, self._append_log)
            self.after(0, lambda: vm_row.btn_one.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_lobby_ui(self, vm_row: "VMRow") -> None:
        vm_row.btn_lobby_ui.configure(state="disabled")
        self._append_log(f"[{vm_row.vm['name']}] Запускаем Лобби UI последовательность...")

        def _run() -> None:
            resp = vm_lobby_ui(vm_row.vm)
            self._append_log(f"[{vm_row.vm['name']}] Лобби UI: {resp}")
            self.after(0, lambda: vm_row.btn_lobby_ui.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_start_lobby(self) -> None:
        """Старт Лобби: VM2 и VM4 выполняют последовательность, коды передаются капитанам VM1 и VM3."""
        if len(self.vm_rows) < 4:
            self._append_log("Ошибка: нужно 4 VM")
            return

        vm1, vm2, vm3, vm4 = [r.vm for r in self.vm_rows]
        self.btn_start_lobby.configure(state="disabled")
        self._append_log("Старт Лобби: выполняем последовательность на VM 2 и VM 4...")

        def _run() -> None:
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f2 = pool.submit(vm_get_friend_code, vm2)
                    f4 = pool.submit(vm_get_friend_code, vm4)
                    resp2 = f2.result()
                    resp4 = f4.result()

                code2 = resp2.get("friend_code", "") if "error" not in resp2 else ""
                code4 = resp4.get("friend_code", "") if "error" not in resp4 else ""

                if "error" in resp2:
                    self._append_log(f"[VM 2] Ошибка: {resp2['error']}")
                if "error" in resp4:
                    self._append_log(f"[VM 4] Ошибка: {resp4['error']}")

                if code2:
                    vm_set_clipboard(vm1, code2)
                    self._append_log(f"Код VM 2 успешно скопирован и передан капитану VM 1: {code2}")
                else:
                    self._append_log("[VM 2] Код не получен (буфер пуст или ошибка)")

                if code4:
                    vm_set_clipboard(vm3, code4)
                    self._append_log(f"Код VM 4 успешно скопирован и передан капитану VM 3: {code4}")
                else:
                    self._append_log("[VM 4] Код не получен (буфер пуст или ошибка)")

                # Запуск последовательности приглашения на капитанах
                self._append_log("Запуск приглашения на VM 1 и VM 3...")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(vm_invite_by_code, vm1, code2) if code2 else None
                    f3 = pool.submit(vm_invite_by_code, vm3, code4) if code4 else None
                    if f1:
                        r1 = f1.result()
                        self._append_log(f"[VM 1] Приглашение: {'OK' if r1.get('ok') else r1.get('error', 'ошибка')}")
                    if f3:
                        r3 = f3.result()
                        self._append_log(f"[VM 3] Приглашение: {'OK' if r3.get('ok') else r3.get('error', 'ошибка')}")

                # VM 2 и VM 4 принимают приглашение
                self._append_log("VM 2 и VM 4 принимают приглашение...")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fa2 = pool.submit(vm_accept_invite, vm2) if code2 else None
                    fa4 = pool.submit(vm_accept_invite, vm4) if code4 else None
                    if fa2:
                        ra2 = fa2.result()
                        self._append_log(f"[VM 2] Принятие: {'OK' if ra2.get('ok') else ra2.get('error', 'ошибка')}")
                    if fa4:
                        ra4 = fa4.result()
                        self._append_log(f"[VM 4] Принятие: {'OK' if ra4.get('ok') else ra4.get('error', 'ошибка')}")

                # Запуск tail console.log на всех 4 VM для детекта match_id
                self._append_log("Запуск мониторинга console.log на всех VM...")
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for vm in [vm1, vm2, vm3, vm4]:
                        pool.submit(vm_start_console_tail, vm, self.cfg)
                self._append_log("Мониторинг запущен. Ожидаем match_id...")

                # VM 1 и VM 3 нажимают Начать
                self._append_log("VM 1 и VM 3 нажимают Начать...")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fs1 = pool.submit(vm_start_match, vm1) if code2 else None
                    fs3 = pool.submit(vm_start_match, vm3) if code4 else None
                    if fs1:
                        rs1 = fs1.result()
                        self._append_log(f"[VM 1] Начать: {'OK' if rs1.get('ok') else rs1.get('error', 'ошибка')}")
                    if fs3:
                        rs3 = fs3.result()
                        self._append_log(f"[VM 3] Начать: {'OK' if rs3.get('ok') else rs3.get('error', 'ошибка')}")
            finally:
                self.after(0, lambda: self.btn_start_lobby.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_win_info(self, vm_row: "VMRow") -> None:
        def _run() -> None:
            resp = vm_win_info(vm_row.vm)
            self._append_log(f"[{vm_row.vm['name']}] Win Info: {resp}")

        threading.Thread(target=_run, daemon=True).start()

    def _on_stop(self) -> None:
        self._append_log("Отправляем команду Logout на все VM...")

        def _do_stop() -> None:
            for row in self.vm_rows:
                resp = vm_logout(row.vm)
                self._append_log(f"[{row.vm['name']}] Logout: {resp}")

        threading.Thread(target=_do_stop, daemon=True).start()

    def _manual_refresh(self) -> None:
        self._poll_vms()

    def _start_polling(self) -> None:
        self._polling = True
        self._poll_cycle()

    def _poll_cycle(self) -> None:
        if not self._polling:
            return
        self._poll_vms()
        self.after(5000, self._poll_cycle)

    def _poll_vms(self) -> None:
        def _fetch(row: VMRow) -> tuple[VMRow, dict]:
            return row, vm_status(row.vm)

        def _run() -> None:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(_fetch, row) for row in self.vm_rows]
                rows_statuses: list[tuple[VMRow, dict]] = []
                for f in as_completed(futures):
                    row, status = f.result()
                    rows_statuses.append((row, status))
                    state = status.get("state", "unreachable")
                    steamid = status.get("steamid", "")
                    error = status.get("error", "")
                    self.after(0, lambda r=row, s=state, sid=steamid, e=error:
                               r.update_status(s, sid, e))
                self._check_match_ids(rows_statuses)

        threading.Thread(target=_run, daemon=True).start()

    def _check_match_ids(self, rows_statuses: list[tuple["VMRow", dict]]) -> None:
        """If all 4 VMs have same match_id, print to console (host)."""
        match_ids: list[tuple[str, dict]] = []
        for row, status in rows_statuses:
            mid = status.get("match_id", "")
            if mid:
                match_ids.append((row.vm["name"], status))
        if len(match_ids) != 4:
            return
        ids = [s.get("match_id") for _, s in match_ids]
        if len(set(ids)) != 1 or not ids[0]:
            return
        match_id = ids[0]
        if match_id == self._last_confirmed_match_id:
            return
        self._last_confirmed_match_id = match_id
        lines = [
            "",
            "=== MATCH_ID CONFIRMED (все 4 VM в одном матче) ===",
            f"match_id: {match_id}",
            "",
        ]
        for vm_name, status in match_ids:
            login = status.get("login", "?")
            extra = status.get("console_extra", {})
            server_steamid = extra.get("server_steamid", "")
            level = extra.get("level", "")
            xp = extra.get("xp", "")
            players = extra.get("players", [])
            parts = [f"  {vm_name}: login={login}"]
            if server_steamid:
                parts.append(f"server_steamid={server_steamid}")
            if level:
                parts.append(f"level={level}")
            if xp:
                parts.append(f"xp={xp}")
            if players:
                parts.append(f"players={players}")
            lines.append(" ".join(parts))
        lines.append("")
        msg = "\n".join(lines)
        log.info(msg)
        print(msg)
        self.after(0, lambda: self._append_log(f"match_id={match_id} — все 4 VM в одном матче"))

    def on_close(self) -> None:
        self._polling = False
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        cfg = load_config()
    except FileNotFoundError:
        print(f"config.json not found at {CONFIG_PATH}")
        sys.exit(1)

    accounts = load_accounts()
    if not accounts:
        print("Аккаунты не найдены — проверьте logpass.txt рядом со скриптом")

    app = CS2FarmApp(cfg, accounts)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
