import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox
from tkinter import ttk

from farm_vm_client import (
    STATE_COLORS,
    STATE_LABELS,
    load_accounts,
    load_config,
    run_2v2_farm,
    run_single_vm,
    vm_accept_invite,
    vm_get_friend_code,
    vm_invite_by_code,
    vm_logout,
    vm_lobby_ui,
    vm_set_clipboard,
    vm_start_console_tail,
    vm_start_match,
    vm_status,
)

# Корень репозитория и пакет бота на VM (импорты в скриптах рассчитаны на cwd = DEMO_ROOT)
_REPO_ROOT = Path(__file__).resolve().parent
_DEMO_ROOT = _REPO_ROOT / "_client" / "_bot"
_TOOL_PREFS_PATH = _REPO_ROOT / ".farm_panel_tool_prefs.json"
_VM_ASSIGN_PATH = _REPO_ROOT / ".farm_panel_vm_assign.json"


def _ai_aim_gain_choices() -> list[tuple[str, list[str]]]:
    """Селектор --aim-gain: шаг 0.1 (1.0 … 3.0) + 1.35 как дефолт из config.py."""
    base = [round(1.0 + 0.1 * i, 1) for i in range(0, 21)]
    vals = sorted(set(base) | {1.35})
    out: list[tuple[str, list[str]]] = []
    for v in vals:
        s = ("%g" % v)
        out.append((s, ["--aim-gain", s]))
    return out


def _ai_aim_max_step_choices() -> list[tuple[str, list[str]]]:
    """Селектор --max-step-logical: шаг 10; 96 (дефолт config) включён в набор."""
    vals = sorted(set(range(40, 201, 10)) | {96})
    return [(str(v), ["--max-step-logical", str(v)]) for v in vals]


def _tool_prefs_load_all() -> dict:
    if not _TOOL_PREFS_PATH.is_file():
        return {"tools": {}}
    try:
        with _TOOL_PREFS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"tools": {}}
        tools = data.get("tools")
        if not isinstance(tools, dict):
            data["tools"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"tools": {}}


def _tool_prefs_save_block(prefs_key: str, labels_to_value: dict[str, str]) -> None:
    data = _tool_prefs_load_all()
    data.setdefault("tools", {})
    data["tools"][prefs_key] = labels_to_value
    try:
        with _TOOL_PREFS_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _batch_quote_path(s: str) -> str:
    """Кавычки для путей внутри .bat (%, ")."""
    return '"' + s.replace("%", "%%").replace('"', '""') + '"'


def _discover_map_names() -> list[str]:
    nav = _DEMO_ROOT / "navmesh"
    if not nav.is_dir():
        return ["de_dust2", "de_poseidon"]
    names = sorted(p.stem for p in nav.glob("*.json") if p.is_file())
    return names if names else ["de_dust2", "de_poseidon"]


class FarmPanelUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CS2 Farm Panel")
        self.geometry("1400x860")
        self.minsize(1200, 740)
        self.configure(bg="#0b1020")

        self._vm_list: list[dict] = self._load_vm_config()
        # Те же аккаунты, что в cs2_farm_controller: logpass.txt + maFiles (farm_vm_client.load_accounts)
        self._accounts: list[dict] = load_accounts()
        self._player_vm: dict[str, int] = {a["login"]: 0 for a in self._accounts}
        self._player_vm_combos: dict[str, ttk.Combobox] = {}
        self._vm_table_rows: list[dict[str, object]] = []
        self._vm_combo_silent = False
        self._farm_thread: threading.Thread | None = None
        self._farm_log_text: tk.Text | None = None
        self._btn_farm_2v2: tk.Button | None = None
        self._btn_farm_start_lobby: tk.Button | None = None
        self._btn_farm_stop: tk.Button | None = None
        self._load_vm_assign_state()

        self._configure_styles()
        self._build_layout()

    def _load_vm_config(self) -> list[dict]:
        """Как в cs2_farm_controller: vms из config.json."""
        default: list[dict] = [
            {"name": f"VM {i}", "id": f"vm{i}", "ip": "—", "port": ""}
            for i in range(1, 5)
        ]
        cfg_path = _REPO_ROOT / "config.json"
        if not cfg_path.is_file():
            return default
        try:
            with cfg_path.open(encoding="utf-8") as f:
                cfg = json.load(f)
            vms = cfg.get("vms")
            if not isinstance(vms, list):
                return default
            out: list[dict] = []
            for i in range(4):
                if i < len(vms) and isinstance(vms[i], dict):
                    out.append(vms[i])
                else:
                    out.append(default[i].copy())
            return out
        except (json.JSONDecodeError, OSError):
            return default

    def _load_vm_assign_state(self) -> None:
        if not _VM_ASSIGN_PATH.is_file():
            return
        try:
            with _VM_ASSIGN_PATH.open(encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("player_vm")
            if not isinstance(raw, dict):
                return
            valid = set(self._player_vm.keys())
            for k, v in raw.items():
                if k not in valid:
                    continue
                if isinstance(v, int) and 0 <= v <= 4:
                    self._player_vm[k] = v
                elif isinstance(v, str) and v.isdigit():
                    iv = int(v)
                    if 0 <= iv <= 4:
                        self._player_vm[k] = iv
        except (json.JSONDecodeError, OSError):
            pass

    def _save_vm_assign_state(self) -> None:
        try:
            with _VM_ASSIGN_PATH.open("w", encoding="utf-8") as f:
                json.dump({"player_vm": self._player_vm}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _vm_choices_for_player(self, player_name: str) -> list[str]:
        """VM 1–4, занятые другими игроками недоступны; «—» = не назначено."""
        mine = self._player_vm.get(player_name, 0)
        taken = {v for p, v in self._player_vm.items() if p != player_name and v}
        opts = ["—"]
        for vm in range(1, 5):
            if vm not in taken or vm == mine:
                opts.append(f"VM {vm}")
        return opts

    def _refresh_player_vm_combos(self) -> None:
        if not self._player_vm_combos:
            return
        self._vm_combo_silent = True
        corrected = False
        try:
            for name, combo in self._player_vm_combos.items():
                choices = self._vm_choices_for_player(name)
                combo.configure(values=choices)
                cur = self._player_vm.get(name, 0)
                disp = "—" if cur == 0 else f"VM {cur}"
                if disp not in choices:
                    self._player_vm[name] = 0
                    disp = "—"
                    corrected = True
                combo.set(disp)
        finally:
            self._vm_combo_silent = False
        if corrected:
            self._save_vm_assign_state()

    def _refresh_vm_cards(self) -> None:
        if not self._vm_table_rows:
            return
        vm_to_player: dict[int, str] = {}
        for pname, vid in self._player_vm.items():
            if vid:
                vm_to_player[vid] = pname
        for idx, row in enumerate(self._vm_table_rows):
            vm_id = idx + 1
            acc = vm_to_player.get(vm_id)
            w_acc = row.get("account_label")
            if isinstance(w_acc, (tk.Label, ttk.Label)):
                w_acc.configure(text=acc or "—")

    def _on_player_vm_selected(self, player_name: str, combo: ttk.Combobox, _: object) -> None:
        if self._vm_combo_silent:
            return
        disp = combo.get()
        new_vm = 0 if disp == "—" else int(disp.split()[-1])
        if new_vm != 0:
            for p in list(self._player_vm):
                if p != player_name and self._player_vm[p] == new_vm:
                    self._player_vm[p] = 0
        self._player_vm[player_name] = new_vm
        self._save_vm_assign_state()
        self._refresh_player_vm_combos()
        self._refresh_vm_cards()

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(
            "Dark.TFrame",
            background="#111827",
            relief="flat",
        )
        style.configure("Root.TFrame", background="#0b1020")
        style.configure(
            "Title.TLabel",
            background="#111827",
            foreground="#ffffff",
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "Label.TLabel",
            background="#111827",
            foreground="#94a3b8",
            font=("Segoe UI", 10),
        )
        style.configure(
            "PlayerName.TLabel",
            background="#1f2937",
            foreground="#ffffff",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "PlayerMeta.TLabel",
            background="#1f2937",
            foreground="#94a3b8",
            font=("Segoe UI", 9),
        )
        style.configure(
            "VMHead.TLabel",
            background="#111827",
            foreground="#94a3b8",
            font=("Segoe UI", 9),
        )
        style.configure(
            "VMMain.TLabel",
            background="#111827",
            foreground="#ffffff",
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "VMSub.TLabel",
            background="#111827",
            foreground="#94a3b8",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Connected.TLabel",
            background="#111827",
            foreground="#4ade80",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Running.TLabel",
            background="#111827",
            foreground="#60a5fa",
            font=("Segoe UI", 9, "bold"),
        )

    def _build_layout(self) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=24)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        left_col = ttk.Frame(root, style="Root.TFrame")
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left_col.rowconfigure(0, weight=1)
        left_col.columnconfigure(0, weight=1)

        right_col = ttk.Frame(root, style="Root.TFrame")
        right_col.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        right_col.rowconfigure(0, weight=0)
        right_col.rowconfigure(1, weight=1)
        right_col.rowconfigure(2, weight=1)
        right_col.rowconfigure(3, weight=0)
        right_col.columnconfigure(0, weight=1)
        right_col.columnconfigure(1, weight=1)

        self._build_settings(left_col)
        self._build_vm_column(right_col)
        self._build_players(right_col)
        self._build_farm_2v2_bar(right_col)

    def _build_settings(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Dark.TFrame", padding=16)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=0)

        ttk.Label(panel, text="⚙ Settings", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        tk.Button(
            panel,
            text="Настройки подключения виртуальных машин",
            command=self._open_vm_connection_settings,
            bg="#3f3f46",
            fg="#ffffff",
            activebackground="#52525b",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=8,
            cursor="hand2",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        gap = (8, 0)
        maps = _discover_map_names()
        map_choices = [(m, ["--map", m]) for m in maps]

        self._build_tool_launch_block(
            panel,
            2,
            "Navigation",
            "navigation",
            _DEMO_ROOT / "nav_demo.py",
            _DEMO_ROOT,
            columns=[
                ("Карта", map_choices),
                (
                    "Источник позиции",
                    [
                        ("Радар (YOLO)", ["--reader", "radar"]),
                        ("Память (pymem)", ["--reader", "memory"]),
                    ],
                ),
                (
                    "Команда",
                    [
                        ("— (без сценария)", []),
                        ("T", ["--team", "T"]),
                        ("CT", ["--team", "CT"]),
                    ],
                ),
                (
                    "Сценарий",
                    [
                        ("—", []),
                        ("rush_a", ["--scenario", "rush_a"]),
                        ("mid_control", ["--scenario", "mid_control"]),
                        ("ramp_execute", ["--scenario", "ramp_execute"]),
                    ],
                ),
                (
                    "Запись waypoints",
                    [("Нет", []), ("Да (--record)", ["--record"])],
                ),
                (
                    "Режим управления",
                    [
                        ("move+look", ["--control-mode", "move+look"]),
                        ("look_only", ["--control-mode", "look_only"]),
                    ],
                ),
                (
                    "Коррекция взгляда на точку",
                    [
                        ("Постоянно (follow)", ["--edge-look-update-mode", "follow"]),
                        ("Один раз (once)", ["--edge-look-update-mode", "once"]),
                    ],
                ),
                (
                    "Авто следующая случайная точка",
                    [
                        ("Нет", ["--auto-random-target", "false"]),
                        ("Да", ["--auto-random-target", "true"]),
                    ],
                ),
            ],
            wrap_pady=(0, 0),
            pre_launch=self._validate_nav_demo_selections,
        )
        self._build_tool_launch_block(
            panel,
            3,
            "NavMesh Editor",
            "navmesh_editor",
            _DEMO_ROOT / "tools" / "navmesh_editor.py",
            _DEMO_ROOT,
            columns=[
                ("Карта", map_choices),
                (
                    "Режим матча",
                    [
                        ("Wingman", ["--mode", "wingman"]),
                        ("Competitive", ["--mode", "competitive"]),
                    ],
                ),
                (
                    "Масштаб окна",
                    [
                        ("4.0", ["--scale", "4.0"]),
                        ("2.0", ["--scale", "2.0"]),
                        ("3.0", ["--scale", "3.0"]),
                        ("5.0", ["--scale", "5.0"]),
                        ("6.0", ["--scale", "6.0"]),
                    ],
                ),
            ],
            wrap_pady=gap,
        )
        self._build_tool_launch_block(
            panel,
            4,
            "AI Aim",
            "ai_aim",
            _DEMO_ROOT / "main.py",
            _DEMO_ROOT,
            columns=[
                (
                    "Команда врагов",
                    [
                        ("CT", ["--team", "CT"]),
                        ("T", ["--team", "T"]),
                    ],
                ),
                ("aim-gain (шаг 0.1)", _ai_aim_gain_choices()),
                ("max-step-logical (шаг 10)", _ai_aim_max_step_choices()),
                (
                    "Превью OpenCV",
                    [
                        ("Да", []),
                        ("Нет", ["--no-preview"]),
                    ],
                ),
            ],
            wrap_pady=gap,
        )

        for r in (2, 3, 4):
            panel.rowconfigure(r, weight=1, uniform="tools")

    @staticmethod
    def _validate_nav_demo_selections(argv: list[str]) -> str | None:
        """nav_demo: --scenario требует --team."""
        if "--scenario" in argv and "--team" not in argv:
            return "Для сценария выберите команду T или CT (не вариант «без сценария»)."
        return None

    def _build_tool_launch_block(
        self,
        panel: ttk.Frame,
        grid_row: int,
        header_text: str,
        prefs_key: str,
        script_path: Path,
        cwd: Path,
        *,
        columns: list[tuple[str, list[tuple[str, list[str]]]]],
        wrap_pady: tuple[int, int] = (0, 0),
        pre_launch: Callable[[list[str]], str | None] | None = None,
    ) -> None:
        nav_wrap = tk.Frame(panel, bg="#111827")
        nav_wrap.grid(row=grid_row, column=0, sticky="nsew", pady=wrap_pady)
        nav_wrap.columnconfigure(0, weight=1)

        header_row = tk.Frame(nav_wrap, bg="#111827")
        header_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header_row.columnconfigure(1, weight=1)

        tk.Label(
            header_row,
            text=header_text,
            bg="#111827",
            fg="#f8fafc",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        preview_lbl = tk.Label(
            header_row,
            text="",
            bg="#111827",
            fg="#64748b",
            font=("Segoe UI", 8),
            anchor="e",
            justify="right",
            wraplength=560,
        )
        preview_lbl.grid(row=0, column=1, sticky="e", padx=(16, 0))

        controls_row = tk.Frame(nav_wrap, bg="#111827")
        controls_row.grid(row=1, column=0, sticky="ew")
        for i in range(len(columns)):
            controls_row.columnconfigure(i, weight=1)

        selectors: list[tuple[ttk.Combobox, dict[str, list[str]]]] = []
        for col_idx, (label, choices) in enumerate(columns):
            combo, arg_by = self._create_select_group(controls_row, col_idx, label, choices)
            selectors.append((combo, arg_by))

        saved_block = _tool_prefs_load_all().get("tools", {}).get(prefs_key, {})
        if isinstance(saved_block, dict):
            for (label, _), (combo, arg_by) in zip(columns, selectors, strict=True):
                want = saved_block.get(label)
                if isinstance(want, str) and want in arg_by:
                    combo.set(want)

        def _persist_selectors() -> None:
            labels_to_value = {
                columns[i][0]: selectors[i][0].get() for i in range(len(columns))
            }
            _tool_prefs_save_block(prefs_key, labels_to_value)

        def collect_argv() -> list[str]:
            extra: list[str] = []
            for combo, arg_by in selectors:
                disp = combo.get()
                extra.extend(arg_by.get(disp, []))
            return extra

        def update_cmd_preview(_: object | None = None) -> None:
            try:
                rel = script_path.relative_to(cwd)
                script_disp = rel.as_posix()
            except ValueError:
                script_disp = script_path.name
            argv = collect_argv()
            tokens = ["python", script_disp, *argv]
            preview_lbl.configure(text=" ".join(shlex.quote(t) for t in tokens))

        def _on_combo_change(_: object | None = None) -> None:
            update_cmd_preview()
            _persist_selectors()

        for combo, _ in selectors:
            combo.bind("<<ComboboxSelected>>", _on_combo_change)
        update_cmd_preview()

        log_outer = tk.Frame(nav_wrap, bg="#111827")
        log_outer.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        log_outer.columnconfigure(0, weight=1)
        log_outer.rowconfigure(0, weight=1)
        nav_wrap.rowconfigure(2, weight=1)

        log_scroll = tk.Scrollbar(log_outer, bg="#0a0a0a", troughcolor="#000000")
        log_text = tk.Text(
            log_outer,
            height=10,
            wrap="char",
            bg="#000000",
            fg="#33ff66",
            insertbackground="#33ff66",
            font=("Consolas", 9),
            highlightthickness=1,
            highlightbackground="#14532d",
            bd=0,
            yscrollcommand=log_scroll.set,
        )
        log_scroll.config(command=log_text.yview)
        log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        proc_holder: list[subprocess.Popen | None] = [None]

        def _set_running_ui(running: bool) -> None:
            start_btn.configure(state="disabled" if running else "normal")
            stop_btn.configure(state="normal" if running else "disabled")
            for combo, _ in selectors:
                combo.configure(state="disabled" if running else "readonly")

        def _append_log_line(line: str) -> None:
            log_text.insert("end", line + "\n")
            log_text.see("end")

        def _finish_reader(code: int | None) -> None:
            proc_holder[0] = None
            _set_running_ui(False)
            sig = "?" if code is None else str(code)
            _append_log_line(f"[выход, код {sig}]")

        def _reader_thread(proc: subprocess.Popen) -> None:
            assert proc.stdout is not None
            try:
                for raw in iter(proc.stdout.readline, ""):
                    if raw == "":
                        break
                    line = raw.rstrip("\n\r")
                    self.after(0, lambda t=line: _append_log_line(t))
            except Exception:
                pass
            try:
                proc.stdout.close()
            except OSError:
                pass
            code = proc.wait()
            self.after(0, lambda c=code: _finish_reader(c))

        def on_stop() -> None:
            proc = proc_holder[0]
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.terminate()
            except OSError:
                pass
            self.after(1500, _force_kill_if_needed)

        def _force_kill_if_needed() -> None:
            proc = proc_holder[0]
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.kill()
            except OSError:
                pass

        def on_start() -> None:
            if proc_holder[0] is not None and proc_holder[0].poll() is None:
                messagebox.showinfo(
                    "Уже запущено",
                    "Сначала остановите процесс кнопкой STOP.",
                    parent=self,
                )
                return
            if not script_path.is_file():
                messagebox.showerror(
                    "Файл не найден",
                    f"Скрипт не найден:\n{script_path}",
                    parent=self,
                )
                return
            extra_argv = collect_argv()
            if pre_launch is not None:
                err = pre_launch(extra_argv)
                if err:
                    messagebox.showwarning("Запуск", err, parent=self)
                    return
            _persist_selectors()
            log_text.delete("1.0", "end")
            try:
                rel = script_path.relative_to(cwd)
                script_disp = rel.as_posix()
            except ValueError:
                script_disp = script_path.name
            shown = " ".join(
                shlex.quote(t)
                for t in ["python", "-u", script_disp, *extra_argv]
            )
            _append_log_line(f"$ cd {shlex.quote(str(cwd))}")
            _append_log_line(f"$ {shown}")
            _append_log_line("—")

            cmd = [sys.executable, "-u", str(script_path), *extra_argv]
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            env = os.environ.copy()
            env.setdefault("PYTHONUTF8", "1")
            env["PYTHONUNBUFFERED"] = "1"
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    creationflags=creationflags,
                )
            except OSError as exc:
                messagebox.showerror("Ошибка запуска", str(exc), parent=self)
                return
            proc_holder[0] = proc
            _set_running_ui(True)
            threading.Thread(
                target=_reader_thread,
                args=(proc,),
                daemon=True,
            ).start()

        def on_clear() -> None:
            log_text.delete("1.0", "end")

        btn_col = tk.Frame(panel, bg="#111827")
        btn_col.grid(
            row=grid_row,
            column=1,
            sticky="s",
            padx=(8, 0),
            pady=wrap_pady,
        )
        start_btn = tk.Button(
            btn_col,
            text="START",
            command=on_start,
            bg="#f59e0b",
            fg="#111827",
            activebackground="#f59e0b",
            activeforeground="#111827",
            bd=0,
            relief="flat",
            font=("Segoe UI", 11, "bold"),
            padx=16,
            pady=12,
            cursor="hand2",
        )
        start_btn.pack(fill="x")
        clear_btn = tk.Button(
            btn_col,
            text="CLEAR",
            command=on_clear,
            bg="#475569",
            fg="#f1f5f9",
            activebackground="#64748b",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=16,
            pady=8,
            cursor="hand2",
        )
        clear_btn.pack(fill="x", pady=(8, 0))
        stop_btn = tk.Button(
            btn_col,
            text="STOP",
            command=on_stop,
            bg="#dc2626",
            fg="#ffffff",
            activebackground="#b91c1c",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 11, "bold"),
            padx=16,
            pady=12,
            cursor="hand2",
            state="disabled",
        )
        stop_btn.pack(fill="x", pady=(8, 0))

    def _open_vm_connection_settings(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("VM connection Settings")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(True, True)
        dlg.geometry("900x620")
        dlg.minsize(860, 560)
        dlg.configure(bg="#e5e7eb")

        outer = tk.Frame(
            dlg,
            bg="#f8fafc",
            highlightthickness=4,
            highlightbackground="#111827",
            highlightcolor="#111827",
        )
        outer.pack(fill="both", expand=True, padx=20, pady=20)

        header = tk.Frame(outer, bg="#f8fafc")
        header.pack(fill="x")

        tk.Label(
            header,
            text="VM connection Settings",
            fg="#ea580c",
            bg="#f8fafc",
            font=("Segoe UI", 18, "bold"),
        ).pack(pady=(16, 12))

        body = tk.Frame(outer, bg="#f8fafc")
        body.pack(fill="both", expand=True)

        grid = tk.Frame(body, bg="#f8fafc")
        grid.pack(fill="both", expand=True, padx=28, pady=(0, 8))
        for c in (0, 1):
            grid.columnconfigure(c, weight=1)

        # Макет: верх — VM1 | VM3, низ — VM2 | VM4
        vm_layout = [
            (0, 0, 1),
            (0, 1, 3),
            (1, 0, 2),
            (1, 1, 4),
        ]
        for row, col, vm_id in vm_layout:
            self._build_vm_connection_block(grid, row, col, vm_id)

        footer = tk.Frame(outer, bg="#f8fafc")
        footer.pack(fill="x", padx=28, pady=(8, 16))

        def save_settings() -> None:
            messagebox.showinfo("Save", "Настройки сохранены.", parent=dlg)
            dlg.destroy()

        save_wrap = tk.Frame(footer, bg="#16a34a", padx=2, pady=2)
        save_wrap.pack(side="right")
        tk.Button(
            save_wrap,
            text="Save",
            command=save_settings,
            fg="#ffffff",
            bg="#15803d",
            activebackground="#166534",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            font=("Segoe UI", 11, "bold"),
            cursor="hand2",
            padx=22,
            pady=10,
        ).pack()

        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.winfo_y() + (self.winfo_height() - dlg.winfo_reqheight()) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _build_vm_connection_block(
        self, parent: tk.Frame, row: int, col: int, vm_id: int
    ) -> None:
        block = tk.Frame(
            parent,
            bg="#ffffff",
            padx=18,
            pady=14,
            highlightthickness=1,
            highlightbackground="#d1d5db",
            highlightcolor="#d1d5db",
        )
        block.grid(row=row, column=col, sticky="nsew", padx=14, pady=12)
        block.columnconfigure(0, weight=1)

        tk.Label(
            block,
            text=f"VM{vm_id}",
            fg="#0f172a",
            bg="#ffffff",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        input_row = tk.Frame(block, bg="#ffffff")
        input_row.grid(row=1, column=0, sticky="ew", pady=4)
        input_row.columnconfigure(1, weight=1)
        tk.Label(
            input_row, text="IP", fg="#0f172a", bg="#ffffff", font=("Segoe UI", 10)
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ip_entry = tk.Entry(
            input_row,
            font=("Segoe UI", 10),
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground="#cbd5e1",
            highlightcolor="#60a5fa",
        )
        ip_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))

        tk.Label(
            input_row, text="port", fg="#0f172a", bg="#ffffff", font=("Segoe UI", 10)
        ).grid(row=0, column=2, sticky="w", padx=(0, 8))
        port_entry = tk.Entry(
            input_row,
            width=8,
            font=("Segoe UI", 10),
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground="#cbd5e1",
            highlightcolor="#60a5fa",
        )
        port_entry.grid(row=0, column=3, sticky="w")

        def check_connection() -> None:
            ip = ip_entry.get().strip()
            port = port_entry.get().strip()
            # Заглушка: сюда можно подставить реальную проверку сокета
            print(f"VM{vm_id} check: {ip}:{port}")

        btn_wrap = tk.Frame(block, bg="#0ea5e9", padx=2, pady=2)
        btn_wrap.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        btn_wrap.columnconfigure(0, weight=1)
        tk.Button(
            btn_wrap,
            text="check connection",
            command=check_connection,
            fg="#ffffff",
            bg="#0284c7",
            activebackground="#0369a1",
            activeforeground="#ffffff",
            relief="raised",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            padx=10,
            pady=8,
        ).grid(row=0, column=0, sticky="ew")

    def _create_select_group(
        self,
        parent: tk.Frame,
        col: int,
        label: str,
        choices: list[tuple[str, list[str]]],
    ) -> tuple[ttk.Combobox, dict[str, list[str]]]:
        """choices: (текст в комбобоксе, фрагмент argv для этого варианта)."""
        group = tk.Frame(parent, bg="#111827")
        group.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0))
        group.columnconfigure(0, weight=1)

        tk.Label(
            group,
            text=label,
            bg="#111827",
            fg="#94a3b8",
            font=("Segoe UI", 9),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        displays = [d for d, _ in choices]
        arg_by_display: dict[str, list[str]] = {d: a for d, a in choices}
        combo_w = max(12, min(22, max(len(d) for d in displays) + 1)) if displays else 12
        combo = ttk.Combobox(
            group, values=displays, state="readonly", width=combo_w
        )
        combo.current(0)
        combo.grid(row=1, column=0, sticky="ew")
        return combo, arg_by_display

    def _build_players(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Dark.TFrame", padding=16)
        panel.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)

        ttk.Label(panel, text="👥 Players", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 12)
        )

        canvas = tk.Canvas(panel, bg="#111827", highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        list_frame = tk.Frame(canvas, bg="#111827")

        list_frame.bind(
            "<Configure>",
            lambda event: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        list_window = canvas.create_window((0, 0), window=list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(list_window, width=event.width),
        )

        if not self._accounts:
            ttk.Label(
                list_frame,
                text="Нет аккаунтов: добавьте строки login:password в logpass.txt "
                "(каталог с config.json), опционально maFiles — как в cs2_farm_controller.",
                style="VMSub.TLabel",
                wraplength=520,
            ).pack(anchor="w", pady=8)
        else:
            for acc in self._accounts:
                row = tk.Frame(list_frame, bg="#1f2937", padx=10, pady=8)
                row.pack(fill="x", pady=4)
                row.columnconfigure(0, weight=1)

                login = acc["login"]
                ttk.Label(row, text=login, style="PlayerName.TLabel").grid(
                    row=0, column=0, sticky="w"
                )

                right = tk.Frame(row, bg="#1f2937")
                right.grid(row=0, column=1, sticky="e")

                fa_txt = "2FA" if acc.get("has_2fa") else "нет 2FA"
                fa_lbl = tk.Label(
                    right,
                    text=fa_txt,
                    bg="#1f2937",
                    fg="#27ae60" if acc.get("has_2fa") else "#e74c3c",
                    font=("Segoe UI", 9),
                )
                fa_lbl.pack(side="left", padx=(0, 8))

                pname = login
                vm_combo = ttk.Combobox(
                    right,
                    width=7,
                    state="readonly",
                    font=("Segoe UI", 9),
                )
                vm_combo.pack(side="left", padx=(10, 0))
                self._player_vm_combos[pname] = vm_combo
                vm_combo.bind(
                    "<<ComboboxSelected>>",
                    lambda _e, n=pname, c=vm_combo: self._on_player_vm_selected(n, c, _e),
                )

        self._refresh_player_vm_combos()
        self._refresh_vm_cards()

    def _build_farm_2v2_bar(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Dark.TFrame", padding=16)
        panel.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        panel.columnconfigure(0, weight=1)

        ttk.Label(panel, text="2v2 ферма", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        bar = tk.Frame(panel, bg="#111827")
        bar.grid(row=1, column=0, sticky="ew")

        self._btn_farm_2v2 = tk.Button(
            bar,
            text="Запустить 2на2",
            command=self._on_farm_start_2v2,
            bg="#1e6fc1",
            fg="#ffffff",
            activebackground="#1557a0",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 11, "bold"),
            padx=16,
            pady=10,
            cursor="hand2",
        )
        self._btn_farm_2v2.pack(side="left", padx=(0, 8))

        self._btn_farm_start_lobby = tk.Button(
            bar,
            text="Старт Лобби",
            command=self._on_farm_start_lobby,
            bg="#8e44ad",
            fg="#ffffff",
            activebackground="#6c3483",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 11, "bold"),
            padx=14,
            pady=10,
            cursor="hand2",
        )
        self._btn_farm_start_lobby.pack(side="left", padx=(0, 8))

        self._btn_farm_stop = tk.Button(
            bar,
            text="Стоп (выход из Steam)",
            command=self._on_farm_stop,
            bg="#7f1717",
            fg="#ffffff",
            activebackground="#5c1010",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 10),
            padx=14,
            pady=10,
            cursor="hand2",
        )
        self._btn_farm_stop.pack(side="left", padx=(0, 8))

        ttk.Label(
            panel,
            text="Капитаны: VM1, VM3  |  Участники: VM2, VM4  |  Старт Лобби после загрузки CS2",
            style="VMSub.TLabel",
            wraplength=560,
        ).grid(row=2, column=0, sticky="w", pady=(10, 6))

        log_fr = tk.Frame(panel, bg="#111827")
        log_fr.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        log_fr.columnconfigure(0, weight=1)

        self._farm_log_text = tk.Text(
            log_fr,
            height=7,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
            bg="#0f172a",
            fg="#e2e8f0",
            insertbackground="#e2e8f0",
            highlightthickness=0,
            bd=0,
        )
        self._farm_log_text.grid(row=0, column=0, sticky="ew")

    def _append_farm_log(self, msg: str) -> None:
        def _do(m: str = msg) -> None:
            w = self._farm_log_text
            if not w or not self.winfo_exists():
                return
            w.configure(state="normal")
            ts = time.strftime("%H:%M:%S")
            w.insert("end", f"[{ts}] {m}\n")
            w.see("end")
            w.configure(state="disabled")

        self.after(0, _do)

    def _collect_farm_assignments(self) -> list[tuple[dict, dict]] | None:
        if len(self._vm_list) < 4:
            self._append_farm_log("Ошибка: нужно 4 VM в config.json")
            return None
        out: list[tuple[dict, dict]] = []
        for idx in range(4):
            vm = self._vm_list[idx]
            acc = self._account_for_vm_index(idx)
            if not acc or not acc.get("login"):
                self._append_farm_log(
                    f"Ошибка: для VM {idx + 1} не назначен аккаунт в блоке Players"
                )
                return None
            out.append((vm, acc))
        for _vm, acc in out:
            if not acc.get("shared_secret"):
                self._append_farm_log(
                    f"Предупреждение: {acc['login']} без 2FA (нет maFile / shared_secret)"
                )
        return out

    def _on_farm_start_2v2(self) -> None:
        if self._farm_thread and self._farm_thread.is_alive():
            self._append_farm_log("Уже запущено!")
            return
        assignments = self._collect_farm_assignments()
        if not assignments:
            return
        try:
            cfg = load_config()
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("config.json", str(exc), parent=self)
            return

        btn = self._btn_farm_2v2
        if btn:
            btn.configure(state="disabled")
        self._append_farm_log("Запускаем 2v2 ферму...")

        def on_log(m: str) -> None:
            self._append_farm_log(m)

        self._farm_thread = threading.Thread(
            target=run_2v2_farm,
            args=(assignments, cfg, on_log),
            daemon=True,
        )
        self._farm_thread.start()

        def _wait_and_enable() -> None:
            if self._farm_thread:
                self._farm_thread.join()
            if btn and self.winfo_exists():
                self.after(0, lambda b=btn: b.configure(state="normal"))

        threading.Thread(target=_wait_and_enable, daemon=True).start()

    def _on_farm_start_lobby(self) -> None:
        if len(self._vm_list) < 4:
            self._append_farm_log("Ошибка: нужно 4 VM")
            return
        vm1, vm2, vm3, vm4 = self._vm_list[0], self._vm_list[1], self._vm_list[2], self._vm_list[3]
        btn = self._btn_farm_start_lobby
        if btn:
            btn.configure(state="disabled")
        self._append_farm_log("Старт Лобби: выполняем последовательность на VM 2 и VM 4...")

        def _run() -> None:
            try:
                try:
                    cfg = load_config()
                except (OSError, json.JSONDecodeError) as exc:
                    self._append_farm_log(f"config.json: {exc}")
                    return

                with ThreadPoolExecutor(max_workers=2) as pool:
                    f2 = pool.submit(vm_get_friend_code, vm2)
                    f4 = pool.submit(vm_get_friend_code, vm4)
                    resp2 = f2.result()
                    resp4 = f4.result()

                code2 = resp2.get("friend_code", "") if "error" not in resp2 else ""
                code4 = resp4.get("friend_code", "") if "error" not in resp4 else ""

                if "error" in resp2:
                    self._append_farm_log(f"[VM 2] Ошибка: {resp2['error']}")
                if "error" in resp4:
                    self._append_farm_log(f"[VM 4] Ошибка: {resp4['error']}")

                if code2:
                    vm_set_clipboard(vm1, code2)
                    self._append_farm_log(f"Код VM 2 передан капитану VM 1: {code2}")
                else:
                    self._append_farm_log("[VM 2] Код не получен (буфер пуст или ошибка)")

                if code4:
                    vm_set_clipboard(vm3, code4)
                    self._append_farm_log(f"Код VM 4 передан капитану VM 3: {code4}")
                else:
                    self._append_farm_log("[VM 4] Код не получен (буфер пуст или ошибка)")

                self._append_farm_log("Запуск приглашения на VM 1 и VM 3...")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(vm_invite_by_code, vm1, code2) if code2 else None
                    f3 = pool.submit(vm_invite_by_code, vm3, code4) if code4 else None
                    if f1:
                        r1 = f1.result()
                        self._append_farm_log(
                            f"[VM 1] Приглашение: {'OK' if r1.get('ok') else r1.get('error', 'ошибка')}"
                        )
                    if f3:
                        r3 = f3.result()
                        self._append_farm_log(
                            f"[VM 3] Приглашение: {'OK' if r3.get('ok') else r3.get('error', 'ошибка')}"
                        )

                self._append_farm_log("VM 2 и VM 4 принимают приглашение...")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fa2 = pool.submit(vm_accept_invite, vm2) if code2 else None
                    fa4 = pool.submit(vm_accept_invite, vm4) if code4 else None
                    if fa2:
                        ra2 = fa2.result()
                        self._append_farm_log(
                            f"[VM 2] Принятие: {'OK' if ra2.get('ok') else ra2.get('error', 'ошибка')}"
                        )
                    if fa4:
                        ra4 = fa4.result()
                        self._append_farm_log(
                            f"[VM 4] Принятие: {'OK' if ra4.get('ok') else ra4.get('error', 'ошибка')}"
                        )

                self._append_farm_log("Запуск мониторинга console.log на всех VM...")
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for vm in (vm1, vm2, vm3, vm4):
                        pool.submit(vm_start_console_tail, vm, cfg)
                self._append_farm_log("Мониторинг запущен. Ожидаем match_id...")

                self._append_farm_log("VM 1 и VM 3 нажимают Начать...")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fs1 = pool.submit(vm_start_match, vm1) if code2 else None
                    fs3 = pool.submit(vm_start_match, vm3) if code4 else None
                    if fs1:
                        rs1 = fs1.result()
                        self._append_farm_log(
                            f"[VM 1] Начать: {'OK' if rs1.get('ok') else rs1.get('error', 'ошибка')}"
                        )
                    if fs3:
                        rs3 = fs3.result()
                        self._append_farm_log(
                            f"[VM 3] Начать: {'OK' if rs3.get('ok') else rs3.get('error', 'ошибка')}"
                        )
            finally:
                if btn and self.winfo_exists():
                    self.after(0, lambda b=btn: b.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_farm_stop(self) -> None:
        self._append_farm_log("Отправляем команду Logout на все VM...")

        def _do_stop() -> None:
            for vm in self._vm_list[:4]:
                resp = vm_logout(vm)
                self._append_farm_log(f"[{vm.get('name', '?')}] Logout: {resp}")

        threading.Thread(target=_do_stop, daemon=True).start()

    def _account_for_vm_index(self, vm_index: int) -> dict | None:
        vm_id = vm_index + 1
        pname = next((p for p, v in self._player_vm.items() if v == vm_id), None)
        if not pname:
            return None
        for a in self._accounts:
            if a["login"] == pname or a["login"].lower() == pname.lower():
                return a
        return None

    def _apply_status_cell(
        self, status_lbl: tk.Label, state: str, steamid: str, err: str
    ) -> None:
        del steamid  # при необходимости вывести в подсказке
        color = STATE_COLORS.get(state, "#555555")
        label = STATE_LABELS.get(state, state)
        if state == "error" and err:
            label = f"Ошибка: {err[:28]}"
        status_lbl.configure(text=label, bg=color, fg="#ffffff")

    def _vm_poll_tick(self) -> None:
        if not self.winfo_exists() or not self._vm_table_rows:
            self.after(5000, self._vm_poll_tick)
            return

        def _work() -> None:
            batch: list[tuple[tk.Label, str, str, str]] = []
            for idx, row in enumerate(self._vm_table_rows):
                lbl = row.get("status_label")
                if not isinstance(lbl, tk.Label):
                    continue
                try:
                    st = vm_status(self._vm_list[idx])
                except Exception as exc:
                    st = {"state": "unreachable", "error": str(exc)}
                state = str(st.get("state", "unreachable"))
                sid = str(st.get("steamid", ""))
                err = str(st.get("error", ""))
                batch.append((lbl, state, sid, err))

            def _apply() -> None:
                for lbl, state, sid, err in batch:
                    self._apply_status_cell(lbl, state, sid, err)

            self.after(0, _apply)

        threading.Thread(target=_work, daemon=True).start()
        self.after(5000, self._vm_poll_tick)

    def _on_vm_one_start(self, vm_index: int) -> None:
        row = self._vm_table_rows[vm_index]
        vm = self._vm_list[vm_index]
        acc = self._account_for_vm_index(vm_index)
        if not acc or not acc.get("login"):
            messagebox.showwarning(
                "One Start",
                "Назначьте игрока на эту VM в блоке Players. "
                "Имя игрока должно совпадать с логином в logpass.txt.",
                parent=self,
            )
            return
        try:
            cfg = load_config()
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("config.json", str(exc), parent=self)
            return
        btn_w = row.get("btn_one")
        if isinstance(btn_w, tk.Button):
            btn_w.configure(state="disabled")

        def _run() -> None:
            try:
                run_single_vm(vm, acc, cfg, on_log=None)
            finally:
                if isinstance(btn_w, tk.Button):
                    self.after(0, lambda b=btn_w: b.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_vm_lobby_ui(self, vm_index: int) -> None:
        row = self._vm_table_rows[vm_index]
        vm = self._vm_list[vm_index]
        btn_w = row.get("btn_lobby_ui")
        if isinstance(btn_w, tk.Button):
            btn_w.configure(state="disabled")

        def _run() -> None:
            try:
                vm_lobby_ui(vm)
            finally:
                if isinstance(btn_w, tk.Button):
                    self.after(0, lambda b=btn_w: b.configure(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _launch_python_script_console(self, script: Path, workdir: Path) -> None:
        """Отдельное окно cmd: скрипт из workdir, по окончании pause (как для fetch/test)."""
        work = str(workdir.resolve())
        scr_s = str(script.resolve())
        py_exe = Path(sys.executable)
        if py_exe.name.lower() == "pythonw.exe":
            py_exe = py_exe.with_name("python.exe")
        py_s = str(py_exe.resolve())
        bat_path: str | None = None
        try:
            if sys.platform == "win32":
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".bat",
                    delete=False,
                    encoding="utf-8",
                    newline="\r\n",
                ) as bf:
                    bf.write("@echo off\r\n")
                    bf.write("setlocal DisableDelayedExpansion\r\n")
                    bf.write(f"cd /d {_batch_quote_path(work)}\r\n")
                    bf.write(f"{_batch_quote_path(py_s)} -u {_batch_quote_path(scr_s)}\r\n")
                    bf.write("echo.\r\n")
                    bf.write("echo ----- завершено -----\r\n")
                    bf.write("pause\r\n")
                    bf.write('del "%~f0"\r\n')
                    bat_path = bf.name
                subprocess.Popen(
                    [
                        "cmd.exe",
                        "/c",
                        "start",
                        "CS2 Farm",
                        "cmd.exe",
                        "/k",
                        bat_path,
                    ],
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    [py_s, "-u", scr_s],
                    cwd=work,
                    start_new_session=True,
                )
        except OSError as exc:
            if bat_path:
                try:
                    os.unlink(bat_path)
                except OSError:
                    pass
            messagebox.showerror("Запуск", str(exc), parent=self)

    def _launch_detection_script(self, script_name: str) -> None:
        """Скрипты из _client/_bot в отдельной консоли."""
        script = _DEMO_ROOT / script_name
        if not script.is_file():
            messagebox.showerror(
                "Файл не найден",
                f"Не найден:\n{script}",
                parent=self,
            )
            return
        self._launch_python_script_console(script, _DEMO_ROOT)

    def _launch_farm_controller(self) -> None:
        """Точка входа — _client_panel.pyw (как _admin_panel.pyw для панели)."""
        launcher = _REPO_ROOT / "_client_panel.pyw"
        script = _REPO_ROOT / "cs2_farm_controller.py"
        if not launcher.is_file():
            messagebox.showerror(
                "Файл не найден",
                f"Не найден лаунчер:\n{launcher}",
                parent=self,
            )
            return
        if not script.is_file():
            messagebox.showerror(
                "Файл не найден",
                f"Не найден контроллер:\n{script}",
                parent=self,
            )
            return
        py_exe = Path(sys.executable)
        if py_exe.name.lower() == "python.exe":
            pw = py_exe.with_name("pythonw.exe")
            if pw.is_file():
                py_exe = pw
        creationflags = 0
        if sys.platform == "win32" and py_exe.name.lower() == "python.exe":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cwd = str(_REPO_ROOT.resolve())
        try:
            subprocess.Popen(
                [str(py_exe.resolve()), str(launcher.resolve())],
                cwd=cwd,
                creationflags=creationflags,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            messagebox.showerror("Запуск контроллера", str(exc), parent=self)

    def _build_vm_column(self, parent: ttk.Frame) -> None:
        toolbar = tk.Frame(parent, bg="#0b1020")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="e", pady=(0, 10))

        btn_row = tk.Frame(toolbar, bg="#0b1020")
        btn_row.pack(side="right")

        # side=right: первым упакованным оказывается правее — порядок: справа оффсеты, память, invite
        tk.Button(
            btn_row,
            text="Обновить оффсеты",
            command=lambda: self._launch_detection_script("fetch_offsets.py"),
            bg="#2d6a2d",
            fg="#ffffff",
            activebackground="#1e4a1e",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
            padx=12,
            pady=8,
            cursor="hand2",
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            btn_row,
            text="Тест памяти",
            command=lambda: self._launch_detection_script("test_memory.py"),
            bg="#7a4f1a",
            fg="#ffffff",
            activebackground="#5a3a12",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
            padx=12,
            pady=8,
            cursor="hand2",
        ).pack(side="right", padx=(8, 0))

        tk.Button(
            btn_row,
            text="Invite Friends",
            command=self._launch_farm_controller,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
            padx=14,
            pady=8,
            cursor="hand2",
        ).pack(side="right", padx=(0, 8))

        grid_host = ttk.Frame(parent, style="Root.TFrame")
        grid_host.grid(row=1, column=0, columnspan=2, sticky="nsew")
        grid_host.columnconfigure(0, weight=1)
        grid_host.rowconfigure(0, weight=1)

        self._build_vm_grid(grid_host)
        self.after(1500, self._vm_poll_tick)

    def _build_vm_grid(self, parent: ttk.Frame) -> None:
        self._vm_table_rows.clear()

        table = ttk.Frame(parent, style="Dark.TFrame", padding=(12, 10))
        table.grid(row=0, column=0, sticky="nsew")
        for col, w in enumerate((0, 1, 0, 1, 0, 0, 0)):
            table.columnconfigure(col, weight=w)

        headers = ("VM", "IP", "Port", "Account", "Status", "One Start", "Лобби UI")
        for col, title in enumerate(headers):
            ttk.Label(table, text=title, style="VMHead.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 8), pady=(0, 10)
            )

        for idx in range(4):
            r = idx + 1
            vm = self._vm_list[idx] if idx < len(self._vm_list) else {}
            ip = str(vm.get("ip", "—"))
            port = vm.get("port", "")
            port_s = "—" if port == "" or port is None else str(port)

            ttk.Label(table, text=f"VM {idx + 1}", style="VMMain.TLabel").grid(
                row=r, column=0, sticky="w", padx=(0, 8), pady=6
            )
            ttk.Label(table, text=ip, style="VMSub.TLabel").grid(
                row=r, column=1, sticky="w", padx=(0, 8), pady=6
            )
            ttk.Label(table, text=port_s, style="VMSub.TLabel").grid(
                row=r, column=2, sticky="w", padx=(0, 8), pady=6
            )
            acc_lbl = ttk.Label(table, text="—", style="VMSub.TLabel")
            acc_lbl.grid(row=r, column=3, sticky="w", padx=(0, 8), pady=6)

            st_lbl = tk.Label(
                table,
                text=STATE_LABELS.get("idle", "idle"),
                font=("Segoe UI", 9, "bold"),
                fg="#ffffff",
                bg=STATE_COLORS.get("idle", "#555555"),
                padx=8,
                pady=4,
            )
            st_lbl.grid(row=r, column=4, sticky="w", padx=(0, 8), pady=6)

            btn_one = tk.Button(
                table,
                text="One Start",
                command=lambda i=idx: self._on_vm_one_start(i),
                bg="#2d6a2d",
                fg="#ffffff",
                activebackground="#1f4f1f",
                activeforeground="#ffffff",
                bd=0,
                relief="flat",
                font=("Segoe UI", 9, "bold"),
                padx=10,
                pady=4,
                cursor="hand2",
            )
            btn_one.grid(row=r, column=5, padx=(0, 6), pady=4)

            btn_lobby = tk.Button(
                table,
                text="Лобби UI",
                command=lambda i=idx: self._on_vm_lobby_ui(i),
                bg="#7b2d8a",
                fg="#ffffff",
                activebackground="#5a1f66",
                activeforeground="#ffffff",
                bd=0,
                relief="flat",
                font=("Segoe UI", 9, "bold"),
                padx=10,
                pady=4,
                cursor="hand2",
            )
            btn_lobby.grid(row=r, column=6, padx=(0, 0), pady=4)

            self._vm_table_rows.append(
                {
                    "account_label": acc_lbl,
                    "status_label": st_lbl,
                    "btn_one": btn_one,
                    "btn_lobby_ui": btn_lobby,
                }
            )


if __name__ == "__main__":
    app = FarmPanelUI()
    app.mainloop()
