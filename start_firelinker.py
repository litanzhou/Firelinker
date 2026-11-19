"""Start the workbench from a source checkout."""

import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3, 11):
        print("Firelinker requires Python 3.11 or newer.", file=sys.stderr)
        return 1

    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    try:
        if not python.is_file():
            print("Creating the Firelinker virtual environment...", flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)

        check = subprocess.run(
            [python, "-I", "-c", "from experience_ledger.web import create_app"],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if check.returncode:
            print("Installing Firelinker dependencies. The first run may take a few minutes.", flush=True)
            subprocess.run(
                [python, "-m", "pip", "install", "-c", "requirements-lock.txt", "-e", ".[files]"],
                cwd=root, check=True,
            )

        return subprocess.call(
            [python, "-m", "experience_ledger", "serve", "--open-browser"], cwd=root,
        )
    except KeyboardInterrupt:
        return 0
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Firelinker could not start. {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
