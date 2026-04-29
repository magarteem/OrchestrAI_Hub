"""Навигация бота по navmesh с чтением позиции из радара или памяти.

Позиция игрока читается двумя способами (--reader, по умолчанию: radar):
  radar   — YOLO-детекция точки игрока на радаре (не требует доступа к памяти)
  memory  — напрямую из памяти cs2.exe (требует pymem, работает только без VAC)

Быстрый старт (radar — режим по умолчанию):
  python nav_demo.py --map de_poseidon
  python nav_demo.py --map de_poseidon --target 42     # идти к узлу id=42
  python nav_demo.py --map de_poseidon --reader memory # читать из памяти

  # Сценарии (scenarios/<map>_vm_runtime.json):
  python nav_demo.py --map de_poseidon --team T --scenario rush_a
  python nav_demo.py --map de_poseidon --team T --scenario mid_control
  python nav_demo.py --map de_poseidon --team T --scenario ramp_execute
  python nav_demo.py --map de_poseidon --control-mode move+look
  python nav_demo.py --map de_poseidon --control-mode look_only

  # Запись маршрута в JSON:
  python nav_demo.py --map de_poseidon --record
  python nav_demo.py --map de_poseidon --record --record-out my_path.json

Требования для режима radar:
  1. Обучи модель:      python -m dataset_tools.trainer --map de_poseidon
  2. Откалибруй радар:  python -m radar_position.calibration.calibrator --map de_poseidon
  3. В игре установи:   cl_radar_rotate 0   cl_radar_always_centered 0

Как узнать ID узла назначения:
  Запусти редактор:  python tools/navmesh_editor.py --map de_poseidon
  Нажми N — появятся номера узлов. Нужный ID передай через --target.

Горячие клавиши:
  P        — пауза / возобновление
  Пробел (удерживать 2 с) — случайный узел как новая цель
  R        — включить / выключить запись waypoints
  Q        — выход
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
import threading
from pathlib import Path
from typing import List, Optional

import keyboard

from navigation import (
    AStarPathfinder,
    MovementController,
    NavmeshGraph,
    NavNode,
    LookController,
    LookConfig,
    reached_waypoint,
)
from position.memory_reader import MemoryPositionReader
from radar_position import RadarPositionReader, RadarConfig
from waypoints.recorder import WaypointRecorder
from config import FovMouseConfig

_LOG = logging.getLogger("nav_demo")

_NAVMESH_DIR = Path(__file__).resolve().parent / "navmesh"

NAV_HZ: float = 100.0
RECORD_AUTOSAVE_EVERY: int = 50  # сохранять JSON каждые N новых точек
EDGE_LOOK_THROTTLE_SEC: float = 0.001
RANDOM_TARGET_MARKER = Path(__file__).resolve().parent / "nav_random_target_id.txt"


# ---------------------------------------------------------------------------

def find_navmesh(map_name: str) -> Path:
    candidates = [
        _NAVMESH_DIR / f"{map_name}.json",
        Path(__file__).resolve().parent / "navmesh" / f"{map_name}.json",
        Path(__file__).resolve().parent / f"{map_name}.json",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(
        f"Navmesh для '{map_name}' не найден. Проверил:\n"
        + "\n".join(f"  {c}" for c in candidates)
    )


def _write_random_target_marker(node_id: int) -> None:
    """Сохраняет id случайной цели для внешней подсветки (navmesh_editor)."""
    try:
        RANDOM_TARGET_MARKER.write_text(str(int(node_id)), encoding="utf-8")
    except OSError:
        # Подсветка вспомогательная, не должна ломать навигацию.
        pass


# ---------------------------------------------------------------------------

class NavBot:
    """Навигационный бот: следует по A*-пути через navmesh."""

    def __init__(
        self,
        graph: NavmeshGraph,
        pathfinder: AStarPathfinder,
        mover: MovementController,
        fov_cfg: FovMouseConfig,
        control_mode: str = "move+look",
        edge_look_update_mode: str = "follow",
    ) -> None:
        self._graph = graph
        self._pathfinder = pathfinder
        self._mover = mover
        self._look = LookController(
            fov_x360=fov_cfg.x360,
            cfg=LookConfig(yaw_dead_zone=6.0, rotation_speed=0.25, max_mouse_step=120),
        )
        self._control_mode = control_mode
        self._edge_look_update_mode = edge_look_update_mode

        self._path: List[NavNode] = []
        self._path_idx: int = 0
        self._active: bool = False
        self._paused: bool = False
        self._combat_paused: bool = False
        self._lock = threading.Lock()
        self._edge_look_key: tuple[int, int, str] | None = None
        self._edge_look_last_apply_ts: float = 0.0
        self._edge_look_once_applied: bool = False

    # ------------------------------------------------------------------
    def set_target_node(self, node_id: int, x: float, y: float, z: float,
                        silent: bool = False) -> bool:
        node = self._graph.get_node(node_id)
        if node is None:
            if not silent:
                _LOG.error("Узел %d не существует в navmesh", node_id)
            return False
        return self._plan_path(x, y, z, node.x, node.y, node.z, silent=silent)

    def set_random_target(self, x: float, y: float, z: float) -> int:
        node_ids = list(self._graph.nodes.keys())
        random.shuffle(node_ids)
        for node_id in node_ids[:20]:
            if self.set_target_node(node_id, x, y, z, silent=True):
                return node_id
        _LOG.warning("Не удалось найти достижимый случайный узел за 20 попыток")
        return -1

    def _plan_path(
        self, fx: float, fy: float, fz: float,
        tx: float, ty: float, tz: float,
        silent: bool = False,
    ) -> bool:
        nodes = self._pathfinder.find_path_from_pos(fx, fy, fz, tx, ty, tz)
        if not nodes:
            if not silent:
                _LOG.warning("Путь не найден")
            return False
        with self._lock:
            self._path = nodes
            self._path_idx = 0
            self._active = True
        _LOG.info(
            "Маршрут: %d вейпоинтов -> узел %d (%.0f, %.0f)",
            len(nodes), nodes[-1].id, nodes[-1].x, nodes[-1].y,
        )
        return True

    # ------------------------------------------------------------------
    def toggle_pause(self) -> None:
        with self._lock:
            self._paused = not self._paused
            paused = self._paused
        if paused:
            self._mover.release_all()
        _LOG.info("Навигация: %s", "ПАУЗА" if paused else "ПРОДОЛЖЕНИЕ")

    def set_combat_pause(self, active: bool) -> None:
        """Пауза из-за обнаружения противника (независима от ручной паузы P)."""
        with self._lock:
            changed = self._combat_paused != active
            self._combat_paused = active
        if changed:
            if active:
                self._mover.release_all()
                _LOG.info("Навигация: ПАУЗА (противник обнаружен)")
            else:
                _LOG.info("Навигация: ПРОДОЛЖЕНИЕ (цель потеряна/убита)")

    def stop(self) -> None:
        with self._lock:
            self._active = False
        self._mover.release_all()

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active and not self._paused and not self._combat_paused

    @property
    def is_blocked(self) -> bool:
        """True, когда навигация заблокирована паузой (ручной или combat)."""
        with self._lock:
            return self._paused or self._combat_paused

    # ------------------------------------------------------------------
    def tick(self, x: float, y: float, z: float, yaw: float) -> None:
        """Один тик навигации. x/y/z — мировые координаты, yaw — в градусах."""
        with self._lock:
            if not self._active or self._paused or self._combat_paused:
                return
            if self._path_idx >= len(self._path):
                self._active = False
                self._mover.stop()
                _LOG.info("Маршрут завершён!")
                return
            target = self._path[self._path_idx]
            next_target = self._path[self._path_idx + 1] if (self._path_idx + 1) < len(self._path) else None

        if reached_waypoint(x, y, target):
            action = target.action
            _LOG.info(
                "Вейпоинт %d/%d (id=%d%s)",
                self._path_idx + 1, len(self._path), target.id,
                f", action={action}" if action else "",
            )
            self._execute_node_action(action)

            with self._lock:
                self._path_idx += 1
                if self._path_idx >= len(self._path):
                    self._active = False
                    self._mover.stop()
                    _LOG.info("Цель достигнута!")
                    return
                target = self._path[self._path_idx]
                next_target = self._path[self._path_idx + 1] if (self._path_idx + 1) < len(self._path) else None

        # look_* на предыдущем узле переопределяет целевой yaw
        from navigation.movement import _LOOK_ANGLES
        prev_action = self._path[self._path_idx - 1].action if self._path_idx > 0 else ""
        prev_node = self._path[self._path_idx - 1] if self._path_idx > 0 else None
        edge_look = None
        if prev_node is not None:
            edge_look = self._graph.get_look_edge(prev_node.id, target.id)

        if prev_action in _LOOK_ANGLES:
            desired_yaw = _LOOK_ANGLES[prev_action]
        else:
            # Если edge-look не задан, ведём взгляд на следующую точку маршрута.
            look_target = next_target if (edge_look is None and next_target is not None) else target
            desired_yaw = self._look.resolve_desired_yaw(x, y, look_target, edge_look)

        apply_look = True
        if edge_look and prev_node is not None:
            edge_mode = str(edge_look.get("mode", "")).strip().lower()
            edge_key = (prev_node.id, target.id, edge_mode)
            now = time.perf_counter()
            if edge_key != self._edge_look_key:
                self._edge_look_key = edge_key
                self._edge_look_last_apply_ts = 0.0
                self._edge_look_once_applied = False
            if self._edge_look_update_mode == "once":
                apply_look = not self._edge_look_once_applied
                if apply_look:
                    self._edge_look_once_applied = True
            else:
                apply_look = (now - self._edge_look_last_apply_ts) >= EDGE_LOOK_THROTTLE_SEC
                if apply_look:
                    self._edge_look_last_apply_ts = now
        else:
            self._edge_look_key = None
            self._edge_look_last_apply_ts = 0.0
            self._edge_look_once_applied = False

        if self._control_mode in ("move+look", "look_only") and apply_look:
            self._look.apply(yaw, desired_yaw)

        # Управление Shift и Ctrl в зависимости от action предыдущего узла
        if self._control_mode in ("move+look", "move_only"):
            if prev_action == "crouch":
                self._mover.crouch_press()
            else:
                self._mover.crouch_release()

            if prev_action == "shift":
                self._mover.shift_press()
            else:
                self._mover.shift_release()

            forward, right = self._movement_axes_for_target(x, y, yaw, target)
            self._mover.move_local(forward=forward, right=right)
        else:
            self._mover.stop()
            self._mover.crouch_release()
            self._mover.shift_release()

    def _movement_axes_for_target(
        self,
        x: float,
        y: float,
        yaw: float,
        target: NavNode,
    ) -> tuple[float, float]:
        dx = target.x - x
        dy = target.y - y
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return (0.0, 0.0)

        dir_x = dx / dist
        dir_y = dy / dist
        yaw_rad = math.radians(yaw)
        fwd_x = math.cos(yaw_rad)
        fwd_y = math.sin(yaw_rad)
        right_x = math.cos(yaw_rad - math.pi / 2.0)
        right_y = math.sin(yaw_rad - math.pi / 2.0)
        forward = dir_x * fwd_x + dir_y * fwd_y
        right = dir_x * right_x + dir_y * right_y
        return (forward, right)

    def _execute_node_action(self, action: str) -> None:
        """Выполняет действие при достижении узла."""
        if action == "jump":
            self._mover.jump()
        elif action in ("look_north", "look_south", "look_east", "look_west"):
            from navigation.movement import _LOOK_ANGLES
            desired = _LOOK_ANGLES[action]
            # yaw неизвестен здесь — поворот выполнится в следующем тике через dyaw
            _LOG.info("look action=%s → target_yaw=%.0f", action, desired)
        # crouch / shift управляются непрерывно в tick(), а не разово


# ---------------------------------------------------------------------------

class ScenarioRunner:
    """Запускает бота по заданному списку вейпоинтов из сценария.

    Переходит к следующему узлу автоматически, когда бот достиг текущего.
    """

    def __init__(self, bot: "NavBot", waypoints: list[int], loop: bool = False) -> None:
        self._bot       = bot
        self._waypoints = waypoints
        self._loop      = loop
        self._idx       = 0
        self._started   = False

    @property
    def finished(self) -> bool:
        return not self._loop and self._idx >= len(self._waypoints)

    def tick(self, x: float, y: float, z: float) -> None:
        if self.finished:
            return
        if self._bot.is_active:
            return  # бот ещё идёт к текущему узлу

        if self._idx >= len(self._waypoints):
            if self._loop:
                self._idx = 0
            else:
                return

        node_id = self._waypoints[self._idx]
        ok = self._bot.set_target_node(node_id, x, y, z, silent=False)
        if ok:
            _LOG.info(
                "Сценарий: узел %d/%d  (id=%d)",
                self._idx + 1, len(self._waypoints), node_id,
            )
            self._idx += 1
        else:
            # Узел недоступен — пропустить
            _LOG.warning("Сценарий: узел id=%d пропущен (недостижим)", node_id)
            self._idx += 1

    @staticmethod
    def from_json(bot: "NavBot", map_name: str, team: str, scenario_name: str) -> "ScenarioRunner":
        """Загрузить сценарий из scenarios/<map>_vm_runtime.json."""
        base = Path(__file__).resolve().parent
        path = base / "scenarios" / f"{map_name}_vm_runtime.json"
        if not path.is_file():
            raise FileNotFoundError(f"Файл сценариев не найден: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        team_key = team.upper()
        scenarios = data.get("scenario_catalog", {}).get(team_key, {})
        if scenario_name not in scenarios:
            available = list(scenarios.keys())
            raise ValueError(
                f"Сценарий '{scenario_name}' для команды {team_key} не найден. "
                f"Доступны: {available}"
            )
        sc = scenarios[scenario_name]
        waypoints = sc["waypoints"]
        loop      = sc.get("loop", False)
        desc      = sc.get("description", "")
        _LOG.info("Сценарий [%s/%s]: %s", team_key, scenario_name, desc)
        _LOG.info("Маршрут: %s", " → ".join(str(w) for w in waypoints))
        return ScenarioRunner(bot, waypoints, loop=loop)


class WaypointRecorderSession:
    """Обёртка над WaypointRecorder с поддержкой on/off и автосохранения."""

    def __init__(self, out_path: Path, map_name: str, dedup_radius: float) -> None:
        self._out = out_path
        if out_path.is_file():
            self._rec = WaypointRecorder.load_merge_points(out_path, map_name=map_name)
            self._rec.dedup_radius = dedup_radius
            _LOG.info("Продолжение записи: %d точек уже есть -> %s", len(self._rec._nodes), out_path)
        else:
            self._rec = WaypointRecorder(map_name=map_name, dedup_radius=dedup_radius)
        self._enabled: bool = True
        self._lock = threading.Lock()
        self._since_save: int = 0

    def toggle(self) -> None:
        with self._lock:
            self._enabled = not self._enabled
        _LOG.info("Запись waypoints: %s", "ВКЛ" if self._enabled else "ВЫКЛ")

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def add(self, x: float, y: float, z: float) -> None:
        with self._lock:
            if not self._enabled:
                return
            nid = self._rec.add_point(x, y, z)
            if nid is not None:
                self._since_save += 1
                total = len(self._rec._nodes)
                _LOG.debug("wp %s записан (всего %d)", nid, total)
                if self._since_save >= RECORD_AUTOSAVE_EVERY:
                    self._rec.save_json(self._out)
                    self._since_save = 0
                    _LOG.info("Автосохранение: %d точек -> %s", total, self._out)

    def save(self) -> None:
        with self._lock:
            self._rec.save_json(self._out)
            _LOG.info("Waypoints сохранены: %d точек -> %s", len(self._rec._nodes), self._out)


# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="CS2 Navmesh navigation demo")
    parser.add_argument("--map", default="de_dust2", help="Название карты")
    parser.add_argument(
        "--reader", choices=["memory", "radar"], default="radar",
        help="Источник позиции: memory — чтение памяти cs2.exe, radar — YOLO по радару (по умолч. radar)",
    )
    parser.add_argument("--target", type=int, default=None, help="ID целевого узла navmesh")
    parser.add_argument(
        "--team", choices=["T", "CT"], default=None,
        help="Команда: T (атака) или CT (защита). Нужен вместе с --scenario",
    )
    parser.add_argument(
        "--scenario", default=None,
        help="Имя сценария из scenarios/<map>_vm_runtime.json (например: rush_a, mid_control)",
    )
    parser.add_argument("--record", action="store_true", help="Записывать путь бота в JSON")
    parser.add_argument(
        "--record-out", default=None,
        help="Путь для сохранения waypoints (по умолч. waypoints/maps/bot_<map>.json)",
    )
    parser.add_argument(
        "--record-dedup", type=float, default=32.0,
        help="Мин. расстояние между точками (по умолч. 32.0)",
    )
    parser.add_argument(
        "--control-mode",
        choices=["move+look", "look_only", "move_only"],
        default="move+look",
        help="Режим управления: move+look (по умолчанию), look_only или move_only",
    )
    parser.add_argument(
        "--edge-look-update-mode",
        choices=["follow", "once"],
        default="follow",
        help="Поведение edge-look: follow — периодическая коррекция, once — навестись один раз",
    )
    parser.add_argument(
        "--auto-random-target",
        choices=["true", "false"],
        default="false",
        help="Автовыбор случайной цели после достижения текущей: true/false",
    )
    args = parser.parse_args()
    auto_random_target = args.auto_random_target == "true"

    # Загрузка navmesh
    try:
        navmesh_path = find_navmesh(args.map)
    except FileNotFoundError as exc:
        _LOG.error("%s", exc)
        return 1

    _LOG.info("Загрузка navmesh: %s", navmesh_path)
    graph = NavmeshGraph(navmesh_path)
    _LOG.info("Navmesh: %d узлов, карта '%s'", len(graph.nodes), graph.map_name)

    pathfinder = AStarPathfinder(graph)
    mover = MovementController()
    fov_cfg = FovMouseConfig()
    bot = NavBot(
        graph,
        pathfinder,
        mover,
        fov_cfg,
        control_mode=args.control_mode,
        edge_look_update_mode=args.edge_look_update_mode,
    )

    # Читалка позиции
    if args.reader == "radar":
        _LOG.info("Режим позиции: YOLO-радар (карта=%s)", args.map)
        reader = RadarPositionReader(RadarConfig(map_name=args.map))
        try:
            reader.attach()
        except FileNotFoundError as exc:
            _LOG.error("%s", exc)
            return 1
    else:
        _LOG.info("Режим позиции: чтение памяти cs2.exe")
        reader = MemoryPositionReader()
        try:
            reader.attach()
        except (ImportError, RuntimeError) as exc:
            _LOG.error("%s", exc)
            _LOG.error("Установи pymem:  pip install pymem")
            return 1

    # Сценарий (опционально)
    scenario_runner: Optional[ScenarioRunner] = None
    if args.scenario:
        if not args.team:
            _LOG.error("Укажи --team T или --team CT вместе с --scenario")
            return 1
        try:
            scenario_runner = ScenarioRunner.from_json(bot, args.map, args.team, args.scenario)
        except (FileNotFoundError, ValueError) as exc:
            _LOG.error("%s", exc)
            return 1

    # Запись waypoints (опционально)
    recorder_session: Optional[WaypointRecorderSession] = None
    if args.record:
        record_out = Path(
            args.record_out
            if args.record_out
            else Path(__file__).parent / "waypoints" / "maps" / f"bot_{args.map}.json"
        )
        recorder_session = WaypointRecorderSession(
            out_path=record_out,
            map_name=args.map,
            dedup_radius=args.record_dedup,
        )
        _LOG.info("Запись waypoints: ВКЛ  (R — вкл/выкл, сохранение при выходе)")

    _LOG.info(
        "Подключено! mode=%s | P — пауза | Пробел (2 с) — случайный узел | R — запись | Q — выход",
        args.control_mode,
    )

    NAV_PAUSE_FLAG = Path(__file__).parent / "nav_pause.flag"

    keyboard.add_hotkey("p", bot.toggle_pause)
    if recorder_session is not None:
        keyboard.add_hotkey("r", recorder_session.toggle)

    start_gate = {"armed": auto_random_target}
    wait_msg_shown = False

    def _on_any_key(event: keyboard.KeyboardEvent) -> None:
        if start_gate["armed"] and event.event_type == "down":
            start_gate["armed"] = False

    keyboard.hook(_on_any_key)

    SPACE_HOLD_SEC = 2.0   # сколько держать пробел для смены цели
    try:
        interval = 1.0 / NAV_HZ
        space_since: float | None = None   # момент начала удержания пробела
        space_fired  = False               # уже сработало в этом удержании
        warned_no_pos = False
        _target_set  = False               # флаг: --target уже передан боту
        _scenario_done = False             # флаг: сценарий завершён

        while True:
            t0 = time.perf_counter()

            if keyboard.is_pressed("q"):
                break

            valid, x, y, z, yaw = reader.snapshot()

            if not valid:
                if not warned_no_pos:
                    _LOG.warning("Позиция не читается. Зайди в матч/тренировку в CS2.")
                    warned_no_pos = True
                time.sleep(0.1)
                continue
            warned_no_pos = False

            # Пробел удерживается 2 с — случайная цель
            if keyboard.is_pressed("space"):
                if space_since is None:
                    space_since = time.perf_counter()
                elif not space_fired and (time.perf_counter() - space_since) >= SPACE_HOLD_SEC:
                    node_id = bot.set_random_target(x, y, z)
                    n = graph.get_node(node_id)
                    if n:
                        _write_random_target_marker(node_id)
                        _LOG.info(
                            "Случайная цель: узел %d (%.0f, %.0f, %.0f)",
                            n.id, n.x, n.y, n.z,
                        )
                    space_fired = True
            else:
                space_since = None
                space_fired = False

            # Сценарий — передаёт следующий вейпоинт когда бот дошёл до предыдущего
            if scenario_runner is not None and not _scenario_done:
                scenario_runner.tick(x, y, z)
                if scenario_runner.finished:
                    _LOG.info("Сценарий завершён!")
                    _scenario_done = True

            # Цель из аргумента командной строки — ставится один раз при старте
            elif args.target is not None and not _target_set:
                bot.set_target_node(args.target, x, y, z)
                _target_set = True

            # Авто-случайная цель: после завершения текущего пути сразу выбрать новую
            if auto_random_target and scenario_runner is None and args.target is None:
                if start_gate["armed"]:
                    if not wait_msg_shown:
                        _LOG.info("Авто-режим: нажми любую клавишу для старта маршрута")
                        wait_msg_shown = True
                elif not bot.is_blocked and not bot.is_active:
                    node_id = bot.set_random_target(x, y, z)
                    n = graph.get_node(node_id)
                    if n:
                        _write_random_target_marker(node_id)
                        _LOG.info(
                            "Авто-случайная цель: узел %d (%.0f, %.0f, %.0f)",
                            n.id, n.x, n.y, n.z,
                        )

            if recorder_session is not None:
                recorder_session.add(x, y, z)

            # Пауза навигации, если main.py обнаружил цель
            bot.set_combat_pause(NAV_PAUSE_FLAG.exists())

            bot.tick(x, y, z, yaw)

            elapsed = time.perf_counter() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    finally:
        bot.stop()
        reader.detach()
        if recorder_session is not None:
            recorder_session.save()
        try:
            keyboard.unhook_all()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
