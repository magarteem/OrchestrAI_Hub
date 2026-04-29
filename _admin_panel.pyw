"""Запуск GUI без консоли (pythonw / двойной клик по .pyw)."""
from pathlib import Path
import runpy

_script = Path(__file__).with_name("cs_2_farm_panel_ui_modern.py")
runpy.run_path(str(_script), run_name="__main__")
