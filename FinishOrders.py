"""Open the company order-finishing site in a dedicated browser profile.

This first implementation deliberately performs no order actions.  It creates
the local journal/profile directories, opens the configured site and keeps the
browser alive so that the operator can sign in manually.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS_PATH = BASE_DIR / "Admin_settings.json"
MUTEX_NAME = "Local\\GreenPoFinishOrdersMutex"
ERROR_ALREADY_EXISTS = 183


def configured_path(value: str, *, relative_to: Path = BASE_DIR) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    return expanded if expanded.is_absolute() else relative_to / expanded


def load_configuration(path: Path) -> dict:
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"Settings file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {path.name}: {error}") from error

    configuration = settings.get("FINISH_ORDERS")
    if not isinstance(configuration, dict):
        raise RuntimeError("FINISH_ORDERS section is missing from Admin_settings.json")
    return configuration


def validate_site_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "Set FINISH_ORDERS.site_url to a complete http:// or https:// address "
            "in Admin_settings.json"
        )
    return url


def append_journal(logs_folder: Path, part_number: int, status: str) -> None:
    logs_folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    journal = logs_folder / f"{now:%d.%m.%Y}.txt"
    with journal.open("a", encoding="utf-8") as file:
        file.write(f"{now:%H:%M:%S} | Part {part_number} | {status}\n")


def acquire_single_instance_mutex():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise RuntimeError("FinishOrders is already running")
    return kernel32, handle


def run(part_number: int, settings_path: Path) -> None:
    configuration = load_configuration(settings_path)
    logs_folder = configured_path(
        str(configuration.get("logs_folder", "~/Desktop/FinishOrdersLogs"))
    )
    profile_folder = configured_path(
        str(configuration.get("browser_profile", "BrowserProfile"))
    )
    logs_folder.mkdir(parents=True, exist_ok=True)
    profile_folder.mkdir(parents=True, exist_ok=True)

    append_journal(logs_folder, part_number, "STARTED")
    site_url = validate_site_url(configuration.get("site_url"))
    browser_channel = str(configuration.get("browser_channel", "chrome")).strip()

    print(f"* FinishOrders journal: {logs_folder}")
    print(f"* FinishOrders browser profile: {profile_folder}")
    print(f"* Opening finishing website for Part {part_number}")
    print("* Sign in manually if the site asks for a login and password")
    print("* Close the FinishOrders browser window when this test is complete")

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_folder),
                channel=browser_channel or "chrome",
                headless=False,
                no_viewport=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(site_url, wait_until="domcontentloaded", timeout=60_000)
                append_journal(logs_folder, part_number, "SITE_OPENED")

                while context.pages:
                    time.sleep(0.5)
            finally:
                context.close()
    except PlaywrightError as error:
        append_journal(logs_folder, part_number, f"ERROR | {error}")
        raise RuntimeError(
            "Cannot open the configured browser. Make sure Google Chrome is "
            "installed, or change FINISH_ORDERS.browser_channel in settings. "
            f"Details: {error}"
        ) from error
    else:
        append_journal(logs_folder, part_number, "BROWSER_CLOSED")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", required=True, type=int)
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
    )
    arguments = parser.parse_args()
    if arguments.part < 1:
        parser.error("--part must be 1 or greater")
    return arguments


def main() -> int:
    arguments = parse_arguments()
    mutex = None
    try:
        mutex = acquire_single_instance_mutex()
        run(arguments.part, arguments.settings.resolve())
        return 0
    except Exception as error:
        print(f"! FinishOrders error: {error}")
        return 1
    finally:
        if mutex is not None:
            kernel32, handle = mutex
            kernel32.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
