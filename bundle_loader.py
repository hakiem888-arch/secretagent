"""Run the original Momentum Radar project from its unchanged source archive.

Only Python's standard library is used here. The archive is checked before it
is extracted; application code, datasets and backend sources are not rewritten.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import importlib.util
from pathlib import Path, PurePosixPath
import runpy
import shutil
import stat
import sys
import tempfile
import threading
from types import ModuleType
import zipfile


PACKAGE_DIR = Path(__file__).resolve().parent
BUNDLE = PACKAGE_DIR / "momentum_radar_bundle.zip"
BUNDLE_SHA256 = "3b8079fa73b685e0856cd6ce8bef01d3c25f5da74a2fdef6b88675db983acc44"
PROJECT_NAME = "freqtrade-develop"
ORIGINAL_FILE_COUNT = 806
_LOCK = threading.Lock()
_APP_LOCK = threading.Lock()
_APP_MODULE: ModuleType | None = None


def _verify_bundle() -> None:
    if not BUNDLE.is_file():
        raise FileNotFoundError(
            "momentum_radar_bundle.zip belum ditemukan. Unggah ZIP internal ini "
            "bersama streamlit_app.py, bundle_loader.py, dan requirements.txt. "
            "ZIP internal tidak perlu diekstrak secara manual."
        )
    with BUNDLE.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != BUNDLE_SHA256:
        raise ValueError(
            "Bundel sumber tidak lengkap atau telah berubah. Gunakan kembali "
            "momentum_radar_bundle.zip dari paket unduhan yang sama."
        )


def _extract_verified_bundle(destination: Path) -> Path:
    """Validate all member paths before writing any archive contents."""
    _verify_bundle()
    with zipfile.ZipFile(BUNDLE) as archive:
        members = archive.infolist()
        file_count = sum(not item.is_dir() for item in members)
        if file_count != ORIGINAL_FILE_COUNT:
            raise ValueError("Jumlah berkas dalam bundel tidak sesuai.")
        root = destination.resolve()
        for item in members:
            path = PurePosixPath(item.filename)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != PROJECT_NAME
                or ".." in path.parts
                or "\\" in item.filename
                or ":" in item.filename
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise ValueError(f"Lokasi berkas bundel tidak valid: {item.filename}")
            if not (root / item.filename).resolve().is_relative_to(root):
                raise ValueError("Lokasi ekstraksi berada di luar folder tujuan.")
        archive.extractall(destination)
    project = destination / PROJECT_NAME
    if not (project / "streamlit_app.py").is_file():
        raise ValueError("Entrypoint aplikasi asli tidak ditemukan.")
    return project


@lru_cache(maxsize=1)
def prepare_project() -> Path:
    """Extract once per source version; publish only complete extractions."""
    with _LOCK:
        runtime = PACKAGE_DIR / ".momentum_runtime"
        version = runtime / BUNDLE_SHA256
        marker = version / ".bundle-ready"
        project = version / PROJECT_NAME
        if marker.is_file() and (project / "streamlit_app.py").is_file():
            if marker.read_text(encoding="utf-8") == BUNDLE_SHA256:
                _verify_bundle()
                return project

        runtime.mkdir(parents=True, exist_ok=True)
        if version.exists():
            raise RuntimeError(
                "Folder runtime tidak lengkap. Hapus folder .momentum_runtime "
                "lalu jalankan ulang aplikasi. Bundel sumber tetap utuh."
            )
        temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=runtime))
        try:
            _extract_verified_bundle(temporary)
            (temporary / ".bundle-ready").write_text(BUNDLE_SHA256, encoding="utf-8")
            try:
                temporary.rename(version)
            except OSError:
                # Another process may have completed the same atomic extraction.
                if not (
                    marker.is_file()
                    and marker.read_text(encoding="utf-8") == BUNDLE_SHA256
                    and (project / "streamlit_app.py").is_file()
                ):
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return project


def activate_project() -> Path:
    project = prepare_project()
    for directory in (project, project / "ft_client"):
        location = str(directory)
        if location not in sys.path:
            sys.path.insert(0, location)
    return project


def run_dashboard() -> None:
    global _APP_MODULE
    project = activate_project()
    # Keep the working directory unchanged: Streamlit secrets/config continue
    # to resolve from the GitHub repository root as usual. The original app
    # resolves its datasets and local client through its own __file__ paths.
    # Load under a distinct name so the upload entrypoint cannot shadow the
    # original. Do not replace process-global __main__ during concurrent sessions.
    with _APP_LOCK:
        if _APP_MODULE is None:
            name = "_momentum_radar_original_entrypoint"
            spec = importlib.util.spec_from_file_location(name, project / "streamlit_app.py")
            if spec is None or spec.loader is None:
                raise ImportError("Entrypoint aplikasi asli tidak dapat dimuat.")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
            _APP_MODULE = module
    _APP_MODULE.run_dashboard_app()


def export_original(destination: Path) -> Path:
    """Recover the complete editable project without overwriting existing files."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(
            f"Folder {destination} sudah ada. Pilih folder baru agar berkas lama tetap aman."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".momentum-export-", dir=destination.parent))
    try:
        project = _extract_verified_bundle(temporary)
        project.rename(destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Pengelola bundel lengkap Momentum Radar")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("verify", help="Periksa checksum dan seluruh isi ZIP")
    subcommands.add_parser("test", help="Jalankan 14 pengujian asli aplikasi")
    unpack = subcommands.add_parser("unpack", help="Pulihkan 806 berkas untuk pengembangan/backend")
    unpack.add_argument("destination", nargs="?", default="momentum-radar-full")
    args = parser.parse_args()
    if args.command == "verify":
        _verify_bundle()
        with zipfile.ZipFile(BUNDLE) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"Berkas rusak: {bad_member}")
            count = sum(not member.is_dir() for member in archive.infolist())
            if count != ORIGINAL_FILE_COUNT:
                raise ValueError("Jumlah berkas tidak sesuai.")
        print(f"OK: {count} berkas asli; SHA-256 sesuai.")
    elif args.command == "test":
        project = activate_project()
        runpy.run_path(str(project / "tests/momentum_radar/run_all_tests.py"), run_name="__main__")
    else:
        print(f"Proyek lengkap dipulihkan ke: {export_original(Path(args.destination))}")


if __name__ == "__main__":
    main()
