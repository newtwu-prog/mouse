"""PyQt6 PC experiment UI. Tkinter entry points stay in pc_ui.py / ui_app.py."""

__all__ = ["main"]


def main() -> int:
    from qt_ui.app import main as run

    return run()
