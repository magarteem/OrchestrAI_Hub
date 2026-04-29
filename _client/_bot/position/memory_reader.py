"""Чтение позиции и угла взгляда игрока напрямую из памяти cs2.exe.

Использует pymem + смещения из offsets.json.
Смещения меняются при каждом обновлении CS2 — обновляй offsets.json из:
  https://github.com/a2x/cs2-dumper/blob/main/output/offsets.json
  https://github.com/a2x/cs2-dumper/blob/main/output/client_dll.hpp
"""

from __future__ import annotations

import json
import logging
import math
import struct
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

_LOG = logging.getLogger(__name__)

_OFFSETS_PATH = Path(__file__).resolve().parent.parent / "offsets.json"
_PROCESS_NAME = "cs2.exe"
_CLIENT_DLL   = "client.dll"


def _load_offsets() -> dict:
    return json.loads(_OFFSETS_PATH.read_text(encoding="utf-8"))


class MemoryPositionReader:
    """Читает позицию и yaw локального игрока через pymem.

    Пример:
        reader = MemoryPositionReader()
        reader.attach()          # один раз при старте
        x, y, z = reader.position
        yaw      = reader.yaw_deg
    """

    def __init__(self) -> None:
        self._pm = None
        self._client_base: int = 0
        self._offsets: dict = {}
        self._lock = threading.Lock()

        self._x: float = 0.0
        self._y: float = 0.0
        self._z: float = 0.0
        self._yaw: float = 0.0
        self._valid: bool = False

        self._thread: Optional[threading.Thread] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    def attach(self) -> None:
        """Подключается к cs2.exe и запускает фоновый поллинг памяти."""
        try:
            import pymem
            import pymem.process
        except ImportError as exc:
            raise ImportError(
                "Установи pymem: pip install pymem"
            ) from exc

        self._offsets = _load_offsets()

        try:
            self._pm = pymem.Pymem(_PROCESS_NAME)
        except pymem.exception.ProcessNotFound:
            raise RuntimeError(
                f"Процесс {_PROCESS_NAME!r} не найден. Запусти CS2 перед запуском бота."
            )

        module = pymem.process.module_from_name(
            self._pm.process_handle, _CLIENT_DLL
        )
        if module is None:
            raise RuntimeError(f"Модуль {_CLIENT_DLL!r} не найден в cs2.exe")

        self._client_base = module.lpBaseOfDll
        _LOG.info(
            "MemoryReader: подключён к cs2.exe, client.dll base=0x%X",
            self._client_base,
        )

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="mem-reader",
        )
        self._thread.start()

    def detach(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        """Читает память ~20 раз в секунду."""
        interval = 0.05
        while self._running:
            t0 = time.perf_counter()
            try:
                self._read_once()
            except Exception as exc:
                _LOG.debug("MemoryReader poll error: %s", exc)
                self._valid = False
            elapsed = time.perf_counter() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def _read_once(self) -> None:
        pm = self._pm
        base = self._client_base
        off = self._offsets

        client_off = off["client.dll"]
        pawn_off    = off["C_BasePlayerPawn"]
        cs_off      = off["C_CSPlayerPawn"]

        # Указатель на pawn (8 байт, little-endian)
        pawn_ptr = pm.read_longlong(base + client_off["dwLocalPlayerPawn"])
        if pawn_ptr == 0:
            self._valid = False
            return

        # Позиция (vec3: 3 × float32)
        x = pm.read_float(pawn_ptr + pawn_off["m_vOldOrigin"])
        y = pm.read_float(pawn_ptr + pawn_off["m_vOldOrigin"] + 4)
        z = pm.read_float(pawn_ptr + pawn_off["m_vOldOrigin"] + 8)

        # Угол взгляда из pawn-структуры: m_angEyeAngles = QAngle(pitch, yaw, roll)
        yaw = pm.read_float(pawn_ptr + cs_off["m_angEyeAngles"] + 4)  # +4 = yaw

        with self._lock:
            self._x = x
            self._y = y
            self._z = z
            self._yaw = yaw
            self._valid = True

    # ------------------------------------------------------------------
    @property
    def valid(self) -> bool:
        with self._lock:
            return self._valid

    @property
    def position(self) -> Tuple[float, float, float]:
        with self._lock:
            return self._x, self._y, self._z

    @property
    def yaw_deg(self) -> float:
        with self._lock:
            return self._yaw

    def snapshot(self) -> Tuple[bool, float, float, float, float]:
        """Возвращает (valid, x, y, z, yaw_deg) атомарно."""
        with self._lock:
            return self._valid, self._x, self._y, self._z, self._yaw
