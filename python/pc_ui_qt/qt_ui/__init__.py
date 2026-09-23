"""PyQt6 PC experiment UI. Tkinter entry point is python/pc_ui_tk/pc_ui.py."""

__all__ = ["main"]


def main() -> int:
    from qt_ui.app import main as run

    return run()
