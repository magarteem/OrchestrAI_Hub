"""Запуск контроллера 2v2 без консоли (pythonw / двойной клик по .pyw)."""
from pathlib import Path
import runpy

_script = Path(__file__).with_name("cs2_farm_controller.py")
runpy.run_path(str(_script), run_name="__main__")
