"""One-command launcher for the OmniGuard hackathon app."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
REQUIRED_MODULES = ("fastapi", "uvicorn", "httpx", "multipart", "pydantic", "pypdf")


def missing_dependencies() -> list[str]:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def install_dependencies() -> None:
    missing = missing_dependencies()
    if not missing:
        print("Required Python packages are already installed.")
        return
    print("First run: installing required Python packages...")
    print("Missing:", ", ".join(missing))
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "-r",
            str(REQUIREMENTS),
        ]
    )


def open_browser() -> None:
    time.sleep(1.8)
    webbrowser.open("http://127.0.0.1:8000/?v=24")


def main() -> None:
    try:
        install_dependencies()
    except subprocess.CalledProcessError:
        print("\nPackage installation failed.")
        print("Confirm that the computer is online, then run this file again.")
        input("Press Enter to close this window...")
        return

    print("\nOmniGuard is starting...")
    print("Website: http://127.0.0.1:8000")
    print("To stop the app, press Ctrl+C in this window.\n")

    threading.Thread(target=open_browser, daemon=True).start()
    result = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        print("\nOmniGuard could not start. The error is shown above.")
        input("Press Enter to close this window...")


if __name__ == "__main__":
    main()
