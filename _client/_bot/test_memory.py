"""Тест чтения позиции из памяти CS2 с подробной диагностикой.

  python test_memory.py         — обычный режим
  python test_memory.py --debug — показывает сырые значения (для диагностики)
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

OFFSETS_FILE = Path(__file__).resolve().parent / "offsets.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Подробный вывод сырых значений")
    args = parser.parse_args()

    print("=" * 60)
    print("CS2 Memory Position Reader — тест")
    print("Ctrl+C — выход")
    print("=" * 60)

    try:
        import pymem
        import pymem.process
    except ImportError:
        print("[ERROR] pymem не установлен. Выполни: pip install pymem")
        sys.exit(1)

    # Загрузка офсетов
    offsets = json.loads(OFFSETS_FILE.read_text(encoding="utf-8"))
    client_off = offsets["client.dll"]
    pawn_off   = offsets["C_BasePlayerPawn"]

    dw_pawn     = client_off["dwLocalPlayerPawn"]
    dw_view_ang = client_off["dwViewAngles"]
    m_origin    = pawn_off["m_vOldOrigin"]

    print(f"\nОфсеты из {OFFSETS_FILE.name}:")
    print(f"  dwLocalPlayerPawn = 0x{dw_pawn:X} ({dw_pawn})")
    print(f"  dwViewAngles      = 0x{dw_view_ang:X} ({dw_view_ang})")
    print(f"  m_vOldOrigin      = 0x{m_origin:X} ({m_origin})\n")

    # Подключение к cs2.exe
    try:
        pm = pymem.Pymem("cs2.exe")
    except pymem.exception.ProcessNotFound:
        print("[ERROR] Процесс cs2.exe не найден. Запусти CS2 и зайди в матч.")
        sys.exit(1)

    module = pymem.process.module_from_name(pm.process_handle, "client.dll")
    if module is None:
        print("[ERROR] client.dll не найден в cs2.exe")
        sys.exit(1)

    base = module.lpBaseOfDll
    print(f"[OK] Подключено. client.dll base = 0x{base:X}\n")

    if args.debug:
        print("[DEBUG] Читаю сырые значения...\n")

        pawn_addr = base + dw_pawn
        print(f"  Адрес pawn-указателя: 0x{pawn_addr:X}")
        pawn_ptr = pm.read_longlong(pawn_addr)
        print(f"  pawn_ptr = 0x{pawn_ptr:X} ({pawn_ptr})")

        if pawn_ptr == 0:
            print("  [!] pawn_ptr = 0 — офсет dwLocalPlayerPawn устарел или игрок ещё не заспавнился")
            print("\n  Попробуй обновить офсеты:")
            print("    python fetch_offsets.py")
        else:
            print(f"  Адрес позиции: 0x{pawn_ptr + m_origin:X}")
            x = pm.read_float(pawn_ptr + m_origin)
            y = pm.read_float(pawn_ptr + m_origin + 4)
            z = pm.read_float(pawn_ptr + m_origin + 8)
            print(f"  pos = ({x:.2f}, {y:.2f}, {z:.2f})")

            va_ptr_addr = base + dw_view_ang
            print(f"\n  Адрес dwViewAngles:  0x{va_ptr_addr:X}")
            print(f"  (не используется — yaw читается из pawn)")
            yaw_off = offsets.get("C_CSPlayerPawn", {}).get("m_angEyeAngles", 15824)
            yaw = pm.read_float(pawn_ptr + yaw_off + 4)
            pitch = pm.read_float(pawn_ptr + yaw_off)
            print(f"  m_angEyeAngles offset = 0x{yaw_off:X}")
            print(f"  pitch={pitch:.2f}, yaw={yaw:.2f}")

        print("\n  Если pawn_ptr=0 или данные неверные — запусти: python fetch_offsets.py")
        return

    # Обычный режим — непрерывный поллинг
    print("Двигайся в CS2 — координаты должны меняться\n")
    no_data_warned = False

    try:
        while True:
            try:
                pawn_ptr = pm.read_longlong(base + dw_pawn)
                if pawn_ptr == 0:
                    if not no_data_warned:
                        print("[!] pawn_ptr=0. Возможно:")
                        print("    1. Ты в главном меню (зайди в матч/тренировку)")
                        print("    2. Офсеты устарели -> python fetch_offsets.py")
                        no_data_warned = True
                    time.sleep(0.5)
                    continue

                x = pm.read_float(pawn_ptr + m_origin)
                y = pm.read_float(pawn_ptr + m_origin + 4)
                z = pm.read_float(pawn_ptr + m_origin + 8)

                m_eye = offsets.get("C_CSPlayerPawn", {}).get("m_angEyeAngles", 15824)
                yaw = pm.read_float(pawn_ptr + m_eye + 4)

                # Простая проверка что значения разумные (не NaN, не 0.0/0.0/0.0)
                if x == 0.0 and y == 0.0 and z == 0.0:
                    if not no_data_warned:
                        print("[!] pos=(0,0,0) — возможно офсет m_vOldOrigin устарел")
                        print("    Запусти: python fetch_offsets.py")
                        no_data_warned = True
                    time.sleep(0.5)
                    continue

                no_data_warned = False
                print(f"pos=({x:8.1f}, {y:8.1f}, {z:6.1f})  yaw={yaw:7.2f}°")

            except Exception as exc:
                print(f"[ERROR] {exc}")
                time.sleep(1)

            time.sleep(0.2)

    except KeyboardInterrupt:
        pass

    print("\nОтключено.")


if __name__ == "__main__":
    main()
