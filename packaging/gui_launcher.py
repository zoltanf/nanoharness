"""PyInstaller entrypoint for the macOS GUI app bundle."""

from nanoharness.desktop import main_desktop


if __name__ == "__main__":
    raise SystemExit(main_desktop())
