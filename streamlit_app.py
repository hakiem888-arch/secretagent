"""Entrypoint GitHub / Streamlit Cloud. The complete original app loads automatically."""

from pathlib import Path
import sys

_package_dir = str(Path(__file__).resolve().parent)
if _package_dir not in sys.path:
    sys.path.insert(0, _package_dir)

from bundle_loader import run_dashboard


if __name__ == "__main__":
    run_dashboard()
