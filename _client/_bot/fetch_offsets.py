"""Загружает актуальные офсеты из cs2-dumper и обновляет offsets.json.

Запускай после каждого обновления CS2:
  python fetch_offsets.py

Источник: https://github.com/a2x/cs2-dumper
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

OFFSETS_JSON_URL  = "https://raw.githubusercontent.com/a2x/cs2-dumper/main/output/offsets.json"
CLIENT_DLL_HPP_URL = "https://raw.githubusercontent.com/a2x/cs2-dumper/main/output/client_dll.hpp"

OUT_FILE = Path(__file__).resolve().parent / "offsets.json"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def parse_hpp_offset(hpp: str, class_name: str, field: str) -> int | None:
    """Извлекает значение из client_dll.hpp: namespace ClassName { ... field = 0xXXXX; }"""
    # Найдём блок нужного класса
    pattern = rf"namespace\s+{re.escape(class_name)}\s*\{{([^}}]*?)\}}"
    block_match = re.search(pattern, hpp, re.DOTALL)
    # Если не нашли точный блок — ищем в контексте (вложенные namespace)
    if not block_match:
        # Fallback: ищем поле глобально
        field_pattern = rf"{re.escape(field)}\s*=\s*(0x[0-9a-fA-F]+|\d+)"
        m = re.search(field_pattern, hpp)
        if m:
            v = m.group(1)
            return int(v, 16) if v.startswith("0x") else int(v)
        return None

    block = block_match.group(1)
    field_pattern = rf"{re.escape(field)}\s*=\s*(0x[0-9a-fA-F]+|\d+)"
    m = re.search(field_pattern, block)
    if m:
        v = m.group(1)
        return int(v, 16) if v.startswith("0x") else int(v)
    return None


def main() -> int:
    print("Загружаю offsets.json из cs2-dumper...")
    try:
        raw_offsets = fetch(OFFSETS_JSON_URL)
    except Exception as exc:
        print(f"[ERROR] offsets.json: {exc}")
        return 1

    offsets_data = json.loads(raw_offsets)
    client = offsets_data.get("client.dll", {})

    dw_pawn      = client.get("dwLocalPlayerPawn")
    dw_view_ang  = client.get("dwViewAngles")

    if dw_pawn is None or dw_view_ang is None:
        print("[ERROR] dwLocalPlayerPawn или dwViewAngles не найдены в offsets.json")
        print("Ключи в client.dll:", list(client.keys()))
        return 1

    print(f"  dwLocalPlayerPawn = {dw_pawn} (0x{dw_pawn:X})")
    print(f"  dwViewAngles      = {dw_view_ang} (0x{dw_view_ang:X})")

    print("\nЗагружаю client_dll.hpp...")
    try:
        hpp = fetch(CLIENT_DLL_HPP_URL)
    except Exception as exc:
        print(f"[ERROR] client_dll.hpp: {exc}")
        return 1

    # Ищем m_vOldOrigin в C_BasePlayerPawn или C_CSPlayerPawn
    m_old_origin = None
    for cls in ("C_BasePlayerPawn", "C_CSPlayerPawn", "C_CSPlayerPawnBase"):
        v = parse_hpp_offset(hpp, cls, "m_vOldOrigin")
        if v is not None:
            m_old_origin = v
            print(f"  {cls}::m_vOldOrigin    = {v} (0x{v:X})")
            break

    # Ищем m_angEyeAngles
    m_eye_ang = None
    for cls in ("C_CSPlayerPawn", "C_BasePlayerPawn", "C_CSPlayerPawnBase"):
        v = parse_hpp_offset(hpp, cls, "m_angEyeAngles")
        if v is not None:
            m_eye_ang = v
            print(f"  {cls}::m_angEyeAngles  = {v} (0x{v:X})")
            break

    if m_old_origin is None:
        print("[WARN] m_vOldOrigin не найден — оставляю прежнее значение")
        try:
            existing = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            m_old_origin = existing.get("C_BasePlayerPawn", {}).get("m_vOldOrigin", 5512)
        except Exception:
            m_old_origin = 5512

    if m_eye_ang is None:
        print("[WARN] m_angEyeAngles не найден — оставляю прежнее значение")
        m_eye_ang = 15824

    result = {
        "_comment": "Автообновлено fetch_offsets.py из https://github.com/a2x/cs2-dumper",
        "client.dll": {
            "dwLocalPlayerPawn": dw_pawn,
            "dwViewAngles":      dw_view_ang,
        },
        "C_BasePlayerPawn": {
            "m_vOldOrigin": m_old_origin,
        },
        "C_CSPlayerPawn": {
            "m_angEyeAngles": m_eye_ang,
        },
    }

    OUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[OK] Сохранено: {OUT_FILE}")
    print("\nТеперь запусти: python test_memory.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
