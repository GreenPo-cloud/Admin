"""Administrative workstation service.

Watches Downloads for order/label PDFs, prints order labels, validates scanner
pairs received over a serial port, photographs valid packages and accepts simple
console commands. Hardware and product mappings live in Admin_settings.json.
"""

from __future__ import annotations

import importlib
import io
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path


def ensure_package(module_name: str, pip_name: str | None = None) -> None:
    """Install a missing dependency into the interpreter running this file."""
    try:
        importlib.import_module(module_name)
    except ImportError:
        package = pip_name or module_name
        print(f"Installing missing package: {package}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])


REQUIRED_PACKAGES = [
    ("cv2", "opencv-python"),
    ("dymo_sdk", "dymo-sdk"),
    ("pdfplumber", "pdfplumber"),
    ("PIL", "Pillow"),
    ("playwright", "playwright"),
    ("pyautogui", "PyAutoGUI"),
    ("pypdf", "pypdf"),
    ("pygame", "pygame"),
    ("pygrabber", "pygrabber"),
    ("numpy", "numpy"),
    ("rapidfuzz", "rapidfuzz"),
    ("requests", "requests"),
    ("rich", "rich"),
    ("send2trash", "send2trash"),
    ("serial", "pyserial"),
    ("watchdog", "watchdog"),
    ("win32event", "pywin32"),
]

for required_module, required_package in REQUIRED_PACKAGES:
    ensure_package(required_module, required_package)


import ctypes
import json

import cv2
import dymo_sdk as dsdk
import pdfplumber
import pygame
import requests
import serial
import win32api
import win32event
import winerror
from pygrabber.dshow_graph import FilterGraph
from pypdf import PdfReader, PdfWriter
from rapidfuzz import fuzz
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from send2trash import send2trash
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "Admin_settings.json"
CURRENT_VERSION = "3.19"
RAW_REPOSITORY_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/Admin/refs/heads/main"
)
VERSION_URL = f"{RAW_REPOSITORY_URL}/version.txt"
PYTHON_URL = f"{RAW_REPOSITORY_URL}/Admin.py"
READ_PUSH_URL = f"{RAW_REPOSITORY_URL}/ReadPush.py"
FINISH_ORDERS_URL = f"{RAW_REPOSITORY_URL}/FinishOrders.py"
PATTERN_URL = f"{RAW_REPOSITORY_URL}/pattern.png"
CHECKBOX_PATTERN_URL = (
    f"{RAW_REPOSITORY_URL}/checkbox.png"
)
SENT_PENDING_PATTERN_URL = (
    f"{RAW_REPOSITORY_URL}/sent%26pending.png"
)
SENT_PAID_PATTERN_URL = (
    f"{RAW_REPOSITORY_URL}/sent%26paid.png"
)
ORDER_IS_SENT_PATTERN_URL = (
    f"{RAW_REPOSITORY_URL}/OrderIsSent.png"
)
COMPLETE_PATTERN_URL = (
    f"{RAW_REPOSITORY_URL}/Complete.png"
)
MUTEX_NAME = "GreenPoAdminProgramMutex"

STATISTICS_PART_HEADER_PATTERN = re.compile(
    r"-+\s*\d{2}\.\d{2}\.\d{4}\s+Part\s+(\d+)\.pdf\s*-+",
    re.IGNORECASE,
)


class FinishOrderValidationError(RuntimeError):
    """The website order does not satisfy the statistics requirements."""


STATISTICS_ANY_ORDER_PATTERN = re.compile(r"(?<!\S)#\d+(?=\s|$)")
UPS_TRACKING_PATTERN = re.compile(r"^1ZR", re.IGNORECASE)
PACKETA_TRACKING_PATTERN = re.compile(r"^Z\d{5,}(?=\s|$)", re.IGNORECASE)
STATISTICS_DELIVERY_ALIASES = {
    "ups": "UPS",
    "zasilkovna": "Packeta",
    "zásilkovna": "Packeta",
    "packeta": "Packeta",
    "postal": "Postal",
}
CHANGE_LABEL_SOURCE_PATTERN = re.compile(
    r"^change\s+(\d+(?:\s*,\s*\d+)*)\.pdf$",
    re.IGNORECASE,
)

COMPANION_FILES = {
    "ReadPush.py": READ_PUSH_URL,
    "FinishOrders.py": FINISH_ORDERS_URL,
    "pattern.png": PATTERN_URL,
    "checkbox.png": CHECKBOX_PATTERN_URL,
    "sent&pending.png": SENT_PENDING_PATTERN_URL,
    "sent&paid.png": SENT_PAID_PATTERN_URL,
    "OrderIsSent.png": ORDER_IS_SENT_PATTERN_URL,
    "Complete.png": COMPLETE_PATTERN_URL,
}

SETTINGS_DEFAULTS = {
    "CAMERA": {
        "name": "Camera 3",
        "focus": 360,
        "width": 4656,
        "height": 3496,
        "warmup_seconds": 2,
        "photo_delay_seconds": 2,
        "idle_timeout_seconds": 10,
    },
    "PRINTER": {
        "name": "DYMO LabelWriter 450 Twin Turbo",
        "order_label": "~/Desktop/order_barcode.dymo",
        "stealth_label": "~/Desktop/stealth_barcode.dymo",
        "roll": 2,
        "delay": 1,
    },
    "COM": {"port": "COM7", "baudrate": 9600},
    "PATHS": {
        "downloads": "~/Downloads",
        "photo_folder": "~/Desktop/RepackFoto",
        "workers": "workers.json",
        "sounds": {
            "good": "~/Desktop/good.mp3",
            "bad": "~/Desktop/bad.mp3",
            "error": "~/Desktop/error.mp3",
            "ups": "~/Desktop/ups.mp3",
        },
    },
    "NETWORK": {
        "downloads": r"\\GREENPO\Downloads",
        "photos": r"\\GREENPO\Photo",
        "statistics": r"\\GREENPO\Statistik2",
    },
    "NOTIFICATION_BOT": {
        "enabled": True,
        "mode": "confirm",
        "allowed_senders": ["Egor Stolyga"],
        "cancel_similarity_threshold": 82,
        "debug_texts": False,
    },
    "FINISH_ORDERS": {
        "site_url": "",
        "browser_channel": "chrome",
        "window_title_contains": "Fast Buds Autoflowering Cannabis Seeds",
        "dropdown_pattern": "pattern.png",
        "pattern_confidence": 0.95,
        "checkbox_pattern": "checkbox.png",
        "red_action_pattern": "sent&pending.png",
        "green_action_pattern": "sent&paid.png",
        "order_is_sent_notification_pattern": "OrderIsSent.png",
        "complete_notification_pattern": "Complete.png",
        "notification_pattern_confidence": 0.60,
        "notification_pattern_edge_inset": 3,
        "menu_pattern_confidence": 0.90,
        "menu_pattern_edge_inset": 1,
        "menu_pattern_scroll_distance": 300,
        "menu_pattern_scroll_attempts": 5,
        "menu_pattern_scroll_settle_delay": 0.35,
        "selection_start_offset": 100,
        "selection_vertical_offset": -10,
        "selection_drag_distance": 60,
        "selection_drag_duration": 0.8,
        "checkbox_vertical_offset": 107,
        "dropdown_edge_inset": 2,
        "mouse_move_duration_min": 0.25,
        "mouse_move_duration_max": 0.75,
        "mouse_speed_multiplier": 2.0,
        "click_delay_min": 0.2,
        "click_delay_max": 1.0,
        "click_delay_scale": 0.5,
        "enter_delay_min": 0.0,
        "enter_delay_max": 1.0,
        "notification_top_min": 140,
        "notification_top_max": 170,
        "notification_right_min": 50,
        "notification_right_max": 250,
        "notification_timeout": 5.0,
        "notification_settle_delay": 0.25,
        "notification_change_threshold": 18,
        "notification_changed_ratio": 0.03,
        "notification_red_score_threshold": 0.12,
        "red_refresh_wait": 5.0,
        "automatic_mode_hold_seconds": 2.0,
        "browser_profile": "BrowserProfile",
        "logs_folder": "~/Desktop/FinishOrdersLogs",
    },
    "ORDERS": {},
}

LEGACY_FINISH_PATTERN_PATHS = {
    "dropdown_pattern": ("~/Desktop/pattern.png", "pattern.png"),
    "checkbox_pattern": ("~/Desktop/checkbox.png", "checkbox.png"),
    "red_action_pattern": (
        "~/Desktop/sent&pending.png",
        "sent&pending.png",
    ),
    "green_action_pattern": ("~/Desktop/sent&paid.png", "sent&paid.png"),
}


def version_key(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", value.strip(), re.IGNORECASE)
    return tuple(map(int, match.group(1).split("."))) if match else None


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"Settings file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {path.name}: {error}") from error

    required = ("CAMERA", "PRINTER", "COM", "PATHS", "NETWORK", "ORDERS")
    missing = [name for name in required if name not in settings]
    if missing:
        raise RuntimeError(f"Missing settings sections: {', '.join(missing)}")
    return settings


def synchronize_setting_values(
    current: dict,
    defaults: dict,
    prefix: str,
) -> tuple[dict, list[str], list[str], list[str]]:
    """Return an independent copy aligned recursively with one settings schema."""
    synchronized: dict = {}
    added: list[str] = []
    removed: list[str] = []
    replaced: list[str] = []

    for key, default_value in defaults.items():
        setting_name = f"{prefix}.{key}" if prefix else key
        if key not in current:
            synchronized[key] = deepcopy(default_value)
            added.append(setting_name)
            continue

        configured_value = current[key]
        if isinstance(default_value, dict):
            if not isinstance(configured_value, dict):
                synchronized[key] = deepcopy(default_value)
                replaced.append(setting_name)
                continue
            nested, nested_added, nested_removed, nested_replaced = (
                synchronize_setting_values(
                    configured_value,
                    default_value,
                    setting_name,
                )
            )
            synchronized[key] = nested
            added.extend(nested_added)
            removed.extend(nested_removed)
            replaced.extend(nested_replaced)
        else:
            synchronized[key] = deepcopy(configured_value)

    removed.extend(
        f"{prefix}.{key}" if prefix else key
        for key in current
        if key not in defaults
    )
    return synchronized, added, removed, replaced


def synchronize_settings(path: Path = SETTINGS_PATH) -> dict:
    """Synchronize the JSON file with the current settings schema."""
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Invalid JSON in {path.name}: {error}") from error
        except OSError as error:
            raise RuntimeError(f"Cannot read {path}: {error}") from error
        if not isinstance(settings, dict):
            raise RuntimeError(f"{path.name} must contain a JSON object")
    else:
        settings = {}

    synchronized, added, removed, replaced = synchronize_setting_values(
        settings,
        SETTINGS_DEFAULTS,
        "",
    )

    # Product mappings are intentionally user-managed and have no fixed keys.
    existing_orders = settings.get("ORDERS")
    if isinstance(existing_orders, dict):
        synchronized["ORDERS"] = deepcopy(existing_orders)
        removed = [
            name for name in removed if not name.startswith("ORDERS.")
        ]
    migrated: list[str] = []
    finish_settings = synchronized.get("FINISH_ORDERS", {})
    if isinstance(finish_settings, dict):
        for key, (legacy_value, current_value) in (
            LEGACY_FINISH_PATTERN_PATHS.items()
        ):
            if finish_settings.get(key) == legacy_value:
                finish_settings[key] = current_value
                migrated.append(f"FINISH_ORDERS.{key}")
    if synchronized != settings:
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(synchronized, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

        changes = []
        if added:
            changes.append(f"added: {', '.join(added)}")
        if removed:
            changes.append(f"removed: {', '.join(removed)}")
        if replaced:
            changes.append(f"replaced invalid objects: {', '.join(replaced)}")
        if migrated:
            changes.append(f"migrated: {', '.join(migrated)}")
        print(f"* {path.name} synchronized ({'; '.join(changes)})")

    return synchronized


def configured_path(value: str) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def statistics_order_match(text: str, order_id: str) -> re.Match[str] | None:
    """Find a whitespace-delimited #OrderNumber anywhere in a statistics line."""
    normalized = order_id.strip()
    return re.search(rf"(?<!\S){re.escape(normalized)}(?=\s|$)", text)


def statistics_order_suffix(text: str, order_id: str) -> str | None:
    """Return assembler/status data following one statistics order number."""
    match = statistics_order_match(text, order_id)
    return text[match.end():] if match is not None else None


def statistics_prefix_fields(
    text: str,
    order_match: re.Match[str] | None = None,
) -> tuple[str | None, str, str]:
    """Return delivery, tracking number and customer from a statistics line.

    New lines begin with an explicit delivery field. Legacy lines begin with
    the tracking number (UPS/Packeta) or indentation (Postal), so both formats
    remain readable.
    """
    order_match = order_match or STATISTICS_ANY_ORDER_PATTERN.search(text)
    if order_match is None:
        return None, "", ""

    prefix = text[:order_match.start()]
    values = prefix.split()
    delivery = (
        STATISTICS_DELIVERY_ALIASES.get(values[0].casefold())
        if values else None
    )
    if delivery is not None:
        values = values[1:]

    tracking_number = ""
    if values and (
        UPS_TRACKING_PATTERN.match(values[0])
        or PACKETA_TRACKING_PATTERN.match(values[0])
    ):
        tracking_number = values.pop(0)

    if delivery is None:
        if UPS_TRACKING_PATTERN.match(tracking_number):
            delivery = "UPS"
        elif PACKETA_TRACKING_PATTERN.match(tracking_number):
            delivery = "Packeta"
        elif prefix[:1].isspace():
            delivery = "Postal"

    return delivery, tracking_number, " ".join(values)


def statistics_order_progress(status_suffix: str) -> tuple[bool, bool]:
    """Return assembled/packed flags from text following an order number."""
    change_markers = list(
        re.finditer(r"(?<!\w)Change(?!\w)", status_suffix, re.IGNORECASE)
    )
    if change_markers:
        status_suffix = status_suffix[change_markers[-1].end():]

    if "+" not in status_suffix:
        return False, False

    after_plus = status_suffix.split("+", 1)[1].strip()
    # A manual completion marker may be written as (+). Its closing bracket is
    # not a packer name. Cancelled is a status marker rather than a name too.
    after_plus = after_plus.lstrip(")]}:;- ").strip()
    after_plus = re.sub(
        r"(?:^|\s+)Cancelled\s*$",
        "",
        after_plus,
        flags=re.IGNORECASE,
    ).strip()
    return True, bool(after_plus)


def locate_statistics_order(
    lines: list[str], order_id: str
) -> tuple[int, int, int | None, int, str, re.Match[str]] | None:
    """Locate an order and its one-based position under the latest Part header."""
    part_number: int | None = None
    number_in_part = 0
    number_without_header = 0

    for line_index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        header = STATISTICS_PART_HEADER_PATTERN.search(text)
        if header is not None:
            part_number = int(header.group(1))
            number_in_part = 0
            continue

        if STATISTICS_ANY_ORDER_PATTERN.search(text) is not None:
            if part_number is None:
                number_without_header += 1
            else:
                number_in_part += 1

        match = statistics_order_match(text, order_id)
        if match is not None:
            order_number = (
                number_in_part if part_number is not None else number_without_header
            )
            return (
                line_index,
                line_index + 1,
                part_number,
                order_number,
                text,
                match,
            )
    return None


def statistics_order_location_text(
    part_number: int | None, order_number: int
) -> str:
    part_text = str(part_number) if part_number is not None else "unknown"
    return f"Part {part_text}, number {order_number}"


def statistics_delivery_type(text: str) -> str | None:
    """Classify a new or legacy statistics order line."""
    delivery, _tracking_number, _customer_name = statistics_prefix_fields(text)
    return delivery


def check_for_updates() -> None:
    """Check once at startup and install only a strictly newer version."""
    try:
        response = requests.get(VERSION_URL, timeout=5)
        response.raise_for_status()
        latest = response.content.decode("utf-8-sig").strip()
        current_key = version_key(CURRENT_VERSION)
        latest_key = version_key(latest)
        if not current_key:
            print(f"! Invalid local Admin version: {CURRENT_VERSION!r}")
            return
        if not latest_key:
            print(f"! Invalid version received from GitHub: {latest!r}")
            return
        if latest_key <= current_key:
            print(f"* Admin {CURRENT_VERSION} is up to date")
            return

        print(f"* Admin update found: {latest}")
        update_program()
    except Exception as error:
        print(f"! Update check failed: {error}")


def download_file(url: str, destination: Path, *, timeout: int = 15) -> None:
    """Download one program file and replace it only after a complete response."""
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    temporary = destination.with_name(f"{destination.name}.download")
    try:
        temporary.write_bytes(response.content)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_companion_files() -> None:
    """Install every missing companion module or image asset at startup."""
    for filename, url in COMPANION_FILES.items():
        destination = BASE_DIR / filename
        if destination.is_file():
            continue
        try:
            download_file(url, destination)
            print(f"* Installed missing companion file: {filename}")
        except Exception as error:
            print(f"! Cannot install {filename}: {error}")


def update_program() -> None:
    downloads = {
        "Admin.py": requests.get(PYTHON_URL, timeout=15),
        **{
            filename: requests.get(url, timeout=15)
            for filename, url in COMPANION_FILES.items()
        },
    }
    for response in downloads.values():
        response.raise_for_status()

    current_file = Path(__file__).resolve()
    batch_file = current_file.with_suffix(".update.bat")
    replacements: list[tuple[Path, Path]] = []
    for filename, response in downloads.items():
        destination = current_file.with_name(filename)
        temporary = destination.with_name(f"{destination.name}.new")
        temporary.write_bytes(response.content)
        replacements.append((temporary, destination))

    move_commands = "".join(
        f'move /Y "{temporary}" "{destination}" >nul\n'
        for temporary, destination in replacements
    )
    batch_file.write_text(
        "@echo off\n"
        "timeout /t 2 >nul\n"
        f"{move_commands}"
        f'start "" "{sys.executable}" "{current_file}"\n'
        'del "%~f0"\n',
        encoding="utf-8",
    )
    print("* Installing update...")
    os.startfile(batch_file)
    raise SystemExit(0)


class ConsoleInput:
    """Keep all stdin reads in the main thread, including print questions."""

    def __init__(self, stop_event: threading.Event):
        self.stop_event = stop_event
        self._lock = threading.Lock()
        self._ask_lock = threading.Lock()
        self._request: tuple[threading.Event, list[str]] | None = None

    def ask(self, prompt: str) -> str | None:
        # Printing and notification confirmation can both ask questions. Keep
        # them sequential so an incoming cancel request is never dropped.
        with self._ask_lock:
            completed = threading.Event()
            answer: list[str] = []
            with self._lock:
                self._request = (completed, answer)
            print(f"\n{prompt}")
            while not self.stop_event.is_set():
                if completed.wait(0.25):
                    return answer[0]
            return None

    def accept_pending_answer(self, text: str) -> bool:
        with self._lock:
            request = self._request
            if request is None:
                return False
            self._request = None
        completed, answer = request
        answer.append(text)
        completed.set()
        return True


class AdminApp:
    def __init__(self, settings: dict):
        self.settings = settings
        self.stop_event = threading.Event()
        self.console_input = ConsoleInput(self.stop_event)
        self.photo_jobs: queue.Queue[tuple[str, str]] = queue.Queue()
        self.pdf_copy_jobs: queue.Queue[Path] = queue.Queue()
        self.notification_jobs: queue.Queue[tuple[str, str]] = queue.Queue()
        self.serial_connection: serial.Serial | None = None
        self.camera: cv2.VideoCapture | None = None
        self.observer: Observer | None = None
        self.threads: list[threading.Thread] = []
        self.print_lock = threading.Lock()
        self.statistics_lock = threading.Lock()
        self.audio: dict[str, pygame.mixer.Sound] = {}
        self.finish_orders_active = False
        self.finish_orders_lock = threading.Lock()
        self.finish_orders_cancel_event = threading.Event()
        self.finish_orders_ignored_dropdowns = 0

        paths = settings["PATHS"]
        self.desktop = Path.home() / "Desktop"
        self.downloads = configured_path(paths.get("downloads", "~/Downloads"))
        self.photo_folder = configured_path(paths.get("photo_folder", "~/Desktop/RepackFoto"))
        self.logs_folder = self.photo_folder / "Logs"
        self.workers_path = configured_path(paths.get("workers", str(BASE_DIR / "workers.json")))
        self.photo_folder.mkdir(parents=True, exist_ok=True)
        self.logs_folder.mkdir(parents=True, exist_ok=True)
        self.workers = json.loads(self.workers_path.read_text(encoding="utf-8"))

    def initialise_audio(self) -> None:
        try:
            pygame.mixer.init()
            sound_paths = self.settings["PATHS"].get("sounds", {})
            for name, path in sound_paths.items():
                resolved = configured_path(path)
                if resolved.exists():
                    self.audio[name] = pygame.mixer.Sound(str(resolved))
                else:
                    print(f"! Sound file not found: {resolved}")
        except Exception as error:
            print(f"! Audio disabled: {error}")

    def play_sound(self, name: str) -> None:
        sound = self.audio.get(name)
        if sound is None:
            return
        try:
            pygame.mixer.stop()
            sound.play()
        except Exception as error:
            print(f"! Cannot play {name} sound: {error}")

    def start(self) -> None:
        self.initialise_audio()
        self.observer = Observer()
        self.observer.schedule(PDFHandler(self), str(self.downloads), recursive=False)
        self.observer.start()
        self._start_thread(self.com_listener, "COM listener")
        self._start_thread(self.camera_worker, "camera worker")
        self._start_thread(self.pdf_copy_worker, "PDF copy worker")
        self._start_thread(self.notification_command_worker, "notification command worker")
        self.start_push_listener()
        print(
            "* Watching Downloads for mpdf.pdf, qwe.pdf, qwez.pdf, "
            "change.pdf and change ORDER.pdf"
        )
        print("* Commands: print part N | send part N | finish part N | cancel [#]1234567 | copy [#]1234567 | check | help | exit")
        self.console_loop()

    def start_push_listener(self) -> None:
        """Start the optional Slack toast listener when ReadPush.py is present."""
        try:
            from ReadPush import start_slack_notification_listener
        except ModuleNotFoundError as error:
            if error.name == "ReadPush":
                print("* ReadPush.py is not installed; Slack push listener is disabled")
            else:
                print(f"! Slack push listener could not be loaded: {error}")
            return
        except Exception as error:
            print(f"! Slack push listener could not be loaded: {error}")
            return

        try:
            configuration = self.settings.get("NOTIFICATION_BOT", {})
            thread = start_slack_notification_listener(
                self.stop_event,
                notification_filter=self.notification_sender_allowed,
                on_notification=self.enqueue_notification,
                debug_texts=bool(configuration.get("debug_texts", False)),
            )
            if thread is not None:
                self.threads.append(thread)
        except Exception as error:
            print(f"! Slack push listener is disabled: {error}")

    def _start_thread(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

    @staticmethod
    def normalize_sender(sender: str) -> str:
        return " ".join(sender.split()).casefold()

    def notification_sender_allowed(self, sender: str) -> bool:
        """Return whether a Windows notification sender is configured."""
        configuration = self.settings.get("NOTIFICATION_BOT", {})
        if not configuration.get("enabled", False):
            return False
        allowed_senders = {
            self.normalize_sender(str(name))
            for name in configuration.get("allowed_senders", [])
        }
        return self.normalize_sender(sender) in allowed_senders

    def enqueue_notification(self, sender: str, message: str) -> None:
        self.notification_jobs.put((sender, message))

    def parse_cancel_notification(self, message: str) -> list[str]:
        """Return unique order IDs only for an unambiguous cancel request."""
        configuration = self.settings.get("NOTIFICATION_BOT", {})
        threshold = float(configuration.get("cancel_similarity_threshold", 82))
        words = re.findall(r"[a-z]+(?:'[a-z]+)?", message.casefold())
        cancel_found = any(fuzz.ratio(word, "cancel") >= threshold for word in words)
        if not cancel_found:
            return []

        # Fail closed: phrases containing a negation are not automatic commands.
        negations = {"not", "never", "dont", "don't", "cannot", "cant", "can't"}
        if any(word in negations for word in words):
            print("! Cancel request ignored because it contains a negation")
            return []

        order_ids: list[str] = []
        seen: set[str] = set()
        for number in re.findall(r"(?<!\d)#?(\d{5,})(?!\d)", message):
            order_id = f"#{number}"
            if order_id not in seen:
                seen.add(order_id)
                order_ids.append(order_id)
        if not order_ids:
            print("! Cancel request contains no order number with at least 5 digits")
        return order_ids

    def notification_command_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                sender, message = self.notification_jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                order_ids = self.parse_cancel_notification(message)
                if not order_ids:
                    continue

                formatted_orders = ", ".join(order_ids)
                configuration = self.settings.get("NOTIFICATION_BOT", {})
                mode = str(configuration.get("mode", "confirm")).casefold()
                if mode == "confirm":
                    while not self.stop_event.is_set():
                        answer = self.console_input.ask(
                            f"Cancel {formatted_orders} requested by {sender}? Enter y or n:"
                        )
                        if answer is None:
                            return
                        normalized_answer = answer.strip().casefold()
                        if normalized_answer in {"y", "yes"}:
                            break
                        if normalized_answer in {"n", "no"}:
                            print(f"* Cancel request rejected: {formatted_orders}")
                            order_ids = []
                            break
                        print("! Please enter y (yes) or n (no)")
                elif mode != "auto":
                    print(
                        f"! Unknown NOTIFICATION_BOT mode '{mode}'; "
                        "use 'confirm' or 'auto'"
                    )
                    continue

                for order_id in order_ids:
                    self.cancel_order_greenpo_manual(order_id)
            except Exception as error:
                print(f"! Cannot process notification command: {error}")
            finally:
                self.notification_jobs.task_done()

    def console_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                command = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if self.console_input.accept_pending_answer(command):
                continue
            self.handle_command(command)
        self.shutdown()

    def handle_command(self, command: str) -> None:
        if not command:
            return
        if command.casefold() in {"exit", "quit"}:
            self.stop_event.set()
            return
        if command.casefold() == "help":
            print("print part N     - print today's order PDF Part N")
            print("send part N      - send today's Label PDF Part N")
            print("finish part N    - finish orders from today's network statistics")
            print("  Right Arrow    - next order; hold 2 seconds for automatic mode")
            print("  Space          - stop the finish cycle and automatic mode")
            print("cancel [#]1234567  - mark an order Cancelled")
            print("copy [#]1234567    - copy matching photos to Desktop")
            print("check               - check completion of today's orders")
            print("exit             - stop Admin")
            return

        if command.casefold() == "check":
            self.check_orders_completion()
            return

        print_match = re.fullmatch(r"print\s+part\s+(\d+)", command, re.IGNORECASE)
        if print_match:
            part_number = int(print_match.group(1))
            if part_number < 1:
                print("! Part number must be 1 or greater")
                return
            self.print_part_manual(part_number)
            return

        send_match = re.fullmatch(r"send\s+part\s+(\d+)", command, re.IGNORECASE)
        if send_match:
            part_number = int(send_match.group(1))
            if part_number < 1:
                print("! Part number must be 1 or greater")
                return
            self.send_label_part_manual(part_number)
            return

        finish_match = re.fullmatch(
            r"finish\s+part\s+(\d+)", command, re.IGNORECASE
        )
        if finish_match:
            part_number = int(finish_match.group(1))
            if part_number < 1:
                print("! Part number must be 1 or greater")
                return
            self.finish_part_manual(part_number)
            return

        cancel_match = re.fullmatch(r"cancel\s+#?(\d+)", command, re.IGNORECASE)
        if cancel_match:
            self.cancel_order_greenpo_manual(f"#{cancel_match.group(1)}")
            return

        copy_match = re.fullmatch(r"copy\s+#?(\d+)", command, re.IGNORECASE)
        if copy_match:
            self.copy_photo_from_network(f"#{copy_match.group(1)}")
            return

        print("! Unknown command. Type: help")

    @staticmethod
    def clipboard_order_number(clipboard_text: str) -> str:
        """Read the first order-sized number from the first non-empty line."""
        first_line = next(
            (line for line in clipboard_text.splitlines() if line.strip()),
            "",
        )
        match = re.search(r"(?<!\d)#?(\d{5,})(?!\d)", first_line)
        if match is None:
            raise FinishOrderValidationError(
                "The first non-empty clipboard line does not contain an order number"
            )
        return match.group(1)

    @staticmethod
    def normalized_customer_name(value: str) -> str:
        return " ".join(value.split()).casefold()

    def validate_finish_order(
        self,
        clipboard_text: str,
        part_number: int,
    ) -> dict:
        """Validate one website row against today's network statistics."""
        statistics_path = self.statistics_path()
        try:
            lines = statistics_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as error:
            raise RuntimeError(
                f"Cannot read finish statistics {statistics_path}: {error}"
            ) from error

        order_number = self.clipboard_order_number(clipboard_text)
        requested_order = f"#{order_number}"
        current_part: int | None = None
        part_found = False
        order_line = ""
        order_match: re.Match[str] | None = None

        for line in lines:
            header = STATISTICS_PART_HEADER_PATTERN.search(line)
            if header is not None:
                current_part = int(header.group(1))
                if current_part == part_number:
                    part_found = True
                elif part_found:
                    break
                continue
            if current_part != part_number:
                continue
            match = statistics_order_match(line, requested_order)
            if match is not None:
                order_line = line
                order_match = match
                break

        if not part_found:
            raise RuntimeError(
                f"Part {part_number} was not found in {statistics_path.name}"
            )
        if order_match is None:
            raise FinishOrderValidationError(
                f"Order {requested_order} was not found in Part {part_number}"
            )

        _delivery, _tracking_number, customer_name = statistics_prefix_fields(
            order_line,
            order_match,
        )
        if not customer_name:
            raise FinishOrderValidationError(
                f"CustomerName is empty for {requested_order} in statistics"
            )

        normalized_clipboard = self.normalized_customer_name(clipboard_text)
        normalized_customer = self.normalized_customer_name(customer_name)
        if normalized_customer not in normalized_clipboard:
            raise FinishOrderValidationError(
                f"CustomerName mismatch for {requested_order}: statistics has "
                f"'{customer_name}'"
            )

        status_suffix = order_line[order_match.end():]
        if re.search(r"(?<!\w)Cancelled(?!\w)", status_suffix, re.IGNORECASE):
            raise FinishOrderValidationError(f"Order {requested_order} is Cancelled")
        assembled, packed = statistics_order_progress(status_suffix)
        if not assembled:
            raise FinishOrderValidationError(f"Order {requested_order} is not assembled")
        if not packed:
            raise FinishOrderValidationError(f"Order {requested_order} is not packed")

        return {
            "order_id": requested_order,
            "customer_name": customer_name,
            "statistics_path": str(statistics_path),
        }

    def activate_finish_orders_browser(self, part_number: int) -> bool:
        """Validate and prepare exactly one website order for finishing."""
        try:
            from FinishOrders import (
                confirm_checkbox_and_mark_action,
                configure_cancel_check,
                configure_interaction_timing,
                open_dropdown_and_mark_checkbox,
                scan_dropdown_patterns,
                select_and_copy_order,
            )

            configuration = self.settings.get("FINISH_ORDERS", {})
            configure_cancel_check(self.finish_orders_cancel_event.is_set)
            click_delay_scale = float(
                configuration.get("click_delay_scale", 0.5)
            )
            if click_delay_scale < 0:
                raise ValueError("click_delay_scale cannot be negative")
            mouse_speed_multiplier = float(
                configuration.get("mouse_speed_multiplier", 2.0)
            )
            if mouse_speed_multiplier <= 0:
                raise ValueError("mouse_speed_multiplier must be greater than zero")
            configure_interaction_timing(
                mouse_move_min=float(
                    configuration.get("mouse_move_duration_min", 0.25)
                ) / mouse_speed_multiplier,
                mouse_move_max=float(
                    configuration.get("mouse_move_duration_max", 0.75)
                ) / mouse_speed_multiplier,
                click_delay_min=float(
                    configuration.get("click_delay_min", 0.2)
                ) * click_delay_scale,
                click_delay_max=float(
                    configuration.get("click_delay_max", 1.0)
                ) * click_delay_scale,
                enter_delay_min=float(
                    configuration.get("enter_delay_min", 0.0)
                ),
                enter_delay_max=float(
                    configuration.get("enter_delay_max", 1.0)
                ),
            )
            title_fragment = str(
                configuration.get(
                    "window_title_contains",
                    "Fast Buds Autoflowering Cannabis Seeds",
                )
            ).strip()
            result = scan_dropdown_patterns(
                title_fragment=title_fragment,
                pattern_path=str(
                    configuration.get(
                        "dropdown_pattern",
                        "pattern.png",
                    )
                ),
                confidence=float(
                    configuration.get("pattern_confidence", 0.95)
                ),
            )
            print(f"* Finishing browser activated: {result['title']}")
            print(f"* Dropdown patterns found: {len(result['matches'])}")
            if not result["matches"]:
                print("! Selection skipped because no dropdown pattern was found")
                return False

            while self.finish_orders_ignored_dropdowns < len(result["matches"]):
                dropdown_index = self.finish_orders_ignored_dropdowns
                selected_match = result["matches"][dropdown_index]
                print(f"* Selecting website Dropdown {dropdown_index + 1}...")
                drag_distance = int(
                    configuration.get("selection_drag_distance", 60)
                )
                clipboard = select_and_copy_order(
                    selected_match,
                    start_offset=int(
                        configuration.get("selection_start_offset", 100)
                    ),
                    vertical_offset=int(
                        configuration.get("selection_vertical_offset", -10)
                    ),
                    drag_distance=drag_distance,
                    drag_duration=float(
                        configuration.get("selection_drag_duration", 0.8)
                    ) / mouse_speed_multiplier,
                )
                print(
                    f"* Selection: ({clipboard['start_x']}, "
                    f"{clipboard['start_y']}) -> ({clipboard['end_x']}, "
                    f"{clipboard['end_y']})"
                )
                print("* Clipboard text:")
                print(clipboard["text"] or "<empty>")
                print(f"* Price: {clipboard['price'] or 'not detected'}")
                print(
                    f"* Price colour: {clipboard['price_colour']} "
                    f"({clipboard['colour_source']})"
                )
                try:
                    validated = self.validate_finish_order(
                        clipboard["text"],
                        part_number,
                    )
                except FinishOrderValidationError as error:
                    Console().print(
                        f"! Dropdown {dropdown_index + 1} ignored: "
                        f"{error}",
                        style="bold yellow",
                    )
                    self.finish_orders_ignored_dropdowns += 1
                    continue
                break
            else:
                print("! No non-ignored Dropdown remains on the current screen")
                return False

            Console().print(
                f"✓ {validated['order_id']} — {validated['customer_name']}: "
                "assembled, packed and ready.",
                style="bold green",
            )
            dropdown = open_dropdown_and_mark_checkbox(
                selected_match,
                result["window_rectangle"],
                checkbox_pattern_path=str(
                    configuration.get(
                        "checkbox_pattern",
                        "checkbox.png",
                    )
                ),
                pattern_confidence=float(
                    configuration.get("menu_pattern_confidence", 0.90)
                ),
                pattern_edge_inset=int(
                    configuration.get("menu_pattern_edge_inset", 1)
                ),
                dropdown_edge_inset=int(
                    configuration.get("dropdown_edge_inset", 2)
                ),
                scroll_distance=int(
                    configuration.get("menu_pattern_scroll_distance", 300)
                ),
                scroll_attempts=int(
                    configuration.get("menu_pattern_scroll_attempts", 5)
                ),
                scroll_settle_delay=float(
                    configuration.get(
                        "menu_pattern_scroll_settle_delay",
                        0.35,
                    )
                ),
            )
            print(
                f"* Dropdown opened at ({dropdown['dropdown_x']}, "
                f"{dropdown['dropdown_y']})"
            )
            print(
                f"* Checkbox pattern target: ({dropdown['checkbox_x']}, "
                f"{dropdown['checkbox_y']}), scrolls: "
                f"{dropdown['checkbox_scroll_count']}"
            )

            action = confirm_checkbox_and_mark_action(
                selected_match,
                dropdown,
                result["window_rectangle"],
                price_colour=clipboard["price_colour"],
                checkbox_vertical_offset=int(
                    configuration.get("checkbox_vertical_offset", 107)
                ),
                dropdown_pattern_path=str(
                    configuration.get("dropdown_pattern", "pattern.png")
                ),
                red_action_pattern_path=str(
                    configuration.get(
                        "red_action_pattern",
                        "sent&pending.png",
                    )
                ),
                green_action_pattern_path=str(
                    configuration.get(
                        "green_action_pattern",
                        "sent&paid.png",
                    )
                ),
                pattern_confidence=float(
                    configuration.get("menu_pattern_confidence", 0.90)
                ),
                pattern_edge_inset=int(
                    configuration.get("menu_pattern_edge_inset", 1)
                ),
                dropdown_edge_inset=int(
                    configuration.get("dropdown_edge_inset", 2)
                ),
                scroll_distance=int(
                    configuration.get("menu_pattern_scroll_distance", 300)
                ),
                scroll_attempts=int(
                    configuration.get("menu_pattern_scroll_attempts", 5)
                ),
                scroll_settle_delay=float(
                    configuration.get(
                        "menu_pattern_scroll_settle_delay",
                        0.35,
                    )
                ),
                order_is_sent_notification_pattern_path=str(
                    configuration.get(
                        "order_is_sent_notification_pattern",
                        "OrderIsSent.png",
                    )
                ),
                complete_notification_pattern_path=str(
                    configuration.get(
                        "complete_notification_pattern",
                        "Complete.png",
                    )
                ),
                notification_pattern_confidence=float(
                    configuration.get("notification_pattern_confidence", 0.60)
                ),
                notification_pattern_edge_inset=int(
                    configuration.get("notification_pattern_edge_inset", 3)
                ),
                notification_top_min=int(
                    configuration.get("notification_top_min", 140)
                ),
                notification_top_max=int(
                    configuration.get("notification_top_max", 170)
                ),
                notification_right_min=int(
                    configuration.get("notification_right_min", 50)
                ),
                notification_right_max=int(
                    configuration.get("notification_right_max", 250)
                ),
                notification_timeout=float(
                    configuration.get("notification_timeout", 5.0)
                ),
                notification_settle_delay=float(
                    configuration.get("notification_settle_delay", 0.25)
                ),
                notification_change_threshold=int(
                    configuration.get("notification_change_threshold", 18)
                ),
                notification_changed_ratio=float(
                    configuration.get("notification_changed_ratio", 0.03)
                ),
                notification_red_score_threshold=float(
                    configuration.get("notification_red_score_threshold", 0.12)
                ),
                red_refresh_wait=float(
                    configuration.get("red_refresh_wait", 5.0)
                ),
            )
            print("* Checkbox clicked and confirmed with Enter")
            print(
                f"* Is Sent notification ({action['sent_notification_pattern']}) "
                f"clicked at ({action['sent_notification_x']}, "
                f"{action['sent_notification_y']}); source: "
                f"{action['sent_notification_source']}, confidence: "
                f"{action['sent_notification_confidence']:.1%}"
            )
            print("* Dropdown reopened")
            print(
                f"* {action['price_colour'].capitalize()} action target: "
                f"({action['action_x']}, {action['action_y']}), pattern: "
                f"{action['action_pattern']}, scrolls: "
                f"{action['action_scroll_count']}"
            )
            print("* Final action clicked and confirmed with Enter")
            print(
                f"* Result notification ({action['notification_pattern']}): "
                f"{action['notification_colour']} via "
                f"{action['notification_source']} "
                f"(confidence {action['notification_confidence']:.1%}, "
                f"red score {action['notification_red_score']:.1%}, "
                f"changed {action['notification_changed_ratio']:.1%})"
            )
            print(
                f"* Result notification clicked at "
                f"({action['notification_x']}, {action['notification_y']})"
            )
            if action["notification_colour"] == "red":
                self.finish_orders_ignored_dropdowns += 1
                Console().print(
                    "! Red result: page refreshed; this Dropdown will be "
                    "ignored.",
                    style="bold red",
                )
            else:
                Console().print(
                    "✓ Green result: order finished successfully.",
                    style="bold green",
                )
            return True
        except Exception as error:
            if self.finish_orders_cancel_event.is_set():
                print("* FinishOrders cycle stopped by Space")
                return False
            print(f"! Finish order skipped: {error}")
            return False

    def print_part_manual(self, part_number: int) -> None:
        today = datetime.now().strftime("%d.%m.%Y")
        pdf_path = self.downloads / f"{today} Part {part_number}.pdf"
        if not pdf_path.is_file():
            print(f"! Today's order PDF was not found: {pdf_path.name}")
            return

        print(f"* Print job accepted: {pdf_path.name}")
        self._start_thread(
            lambda: self.process_order_pdf(pdf_path),
            f"print Part {part_number}",
        )

    def send_label_part_manual(self, part_number: int) -> None:
        today = datetime.now().strftime("%d.%m.%Y")
        label_path = self.downloads / f"{today} Part {part_number} (Label).pdf"
        if not label_path.is_file():
            print(f"! Today's Label PDF was not found: {label_path.name}")
            return

        self.send_pdf(label_path)
        print(f"* Label send job accepted: {label_path.name}")

    def finish_part_manual(self, part_number: int) -> None:
        """Start a key-stepped finishing session without blocking the console."""
        with self.finish_orders_lock:
            if self.finish_orders_active:
                print("! A finish part session is already active")
                return
            self.finish_orders_active = True
            self.finish_orders_cancel_event.clear()
            self.finish_orders_ignored_dropdowns = 0

        statistics_path = self.statistics_path()
        try:
            if not statistics_path.is_file():
                raise FileNotFoundError(statistics_path)
        except OSError as error:
            with self.finish_orders_lock:
                self.finish_orders_active = False
            print(f"! Today's network statistics file is unavailable: {error}")
            return

        self._start_thread(
            lambda: self.finish_part_key_session(part_number),
            f"finish Part {part_number}",
        )
        print(f"* FinishOrders started for Part {part_number}")

    def finish_part_key_session(self, part_number: int) -> None:
        """Run manual steps or switch to continuous mode by holding Right."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        vk_right = 0x27
        configuration = self.settings.get("FINISH_ORDERS", {})

        try:
            automatic_hold_seconds = float(
                configuration.get("automatic_mode_hold_seconds", 2.0)
            )
            if automatic_hold_seconds <= 0:
                raise ValueError(
                    "automatic_mode_hold_seconds must be greater than zero"
                )
            stop_key_thread = threading.Thread(
                target=self.monitor_finish_stop_key,
                args=(user32,),
                name="finish Space listener",
                daemon=True,
            )
            stop_key_thread.start()
            print(f"* Network statistics: {self.statistics_path()}")
            first_order_finished = self.activate_finish_orders_browser(part_number)
            if self.finish_orders_cancel_event.is_set():
                return
            if not first_order_finished:
                print("* FinishOrders stopped: no order was found")
                return

            print(
                "* Release Right Arrow for one order, or hold it for "
                f"{automatic_hold_seconds:g} seconds to enable automatic mode"
            )
            automatic_mode = False
            right_down_since: float | None = (
                time.monotonic()
                if user32.GetAsyncKeyState(vk_right) & 0x8000
                else None
            )
            while (
                not self.finish_orders_cancel_event.is_set()
                and not self.stop_event.wait(0.03)
            ):
                if automatic_mode:
                    order_finished = self.activate_finish_orders_browser(part_number)
                    if self.finish_orders_cancel_event.is_set():
                        return
                    if not order_finished:
                        print(
                            "* Automatic mode stopped: the next order was not found"
                        )
                        return
                    continue

                is_down = bool(user32.GetAsyncKeyState(vk_right) & 0x8000)
                if is_down:
                    if right_down_since is None:
                        right_down_since = time.monotonic()
                    elif (
                        time.monotonic() - right_down_since
                        >= automatic_hold_seconds
                    ):
                        automatic_mode = True
                        Console().print(
                            "▶ Automatic finish mode enabled",
                            style="bold cyan",
                        )
                    continue

                if right_down_since is not None:
                    held_seconds = time.monotonic() - right_down_since
                    right_down_since = None
                    if held_seconds >= automatic_hold_seconds:
                        automatic_mode = True
                        Console().print(
                            "▶ Automatic finish mode enabled",
                            style="bold cyan",
                        )
                        continue
                    order_finished = self.activate_finish_orders_browser(part_number)
                    if self.finish_orders_cancel_event.is_set():
                        return
                    if not order_finished:
                        print("* FinishOrders stopped: the next order was not found")
                        return
                    print(
                        "* Release Right Arrow for one order, or hold it for "
                        f"{automatic_hold_seconds:g} seconds to enable "
                        "automatic mode"
                    )
        except Exception as error:
            print(f"! FinishOrders session stopped: {error}")
        finally:
            with self.finish_orders_lock:
                self.finish_orders_active = False

    def monitor_finish_stop_key(self, user32) -> None:
        """Stop the active finish session as soon as Space is pressed."""
        vk_space = 0x20
        was_down = bool(user32.GetAsyncKeyState(vk_space) & 0x8000)
        while not self.stop_event.wait(0.02):
            with self.finish_orders_lock:
                if not self.finish_orders_active:
                    return
            is_down = bool(user32.GetAsyncKeyState(vk_space) & 0x8000)
            if not was_down and is_down:
                self.finish_orders_cancel_event.set()
                print("* Space pressed — automatic mode and FinishOrders stopping")
                return
            was_down = is_down

    def copy_with_retry(self, file_path: Path) -> None:
        network_folder = Path(self.settings["NETWORK"]["downloads"])
        destination = network_folder / file_path.name
        while not self.stop_event.is_set():
            try:
                shutil.copy2(file_path, destination)
                print(f"* Copied to second computer: {destination}")
                return
            except Exception as error:
                print(f"! Copy failed: {error}; retrying in 5 seconds")
                self.stop_event.wait(5)

    def send_pdf(self, file_path: Path) -> None:
        # One worker preserves the order of PDF copy requests.
        self.pdf_copy_jobs.put(file_path)

    def pdf_copy_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                file_path = self.pdf_copy_jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.copy_with_retry(file_path)
            finally:
                self.pdf_copy_jobs.task_done()

    def copy_photo_from_network(self, order_id: str) -> int:
        network_folder = Path(self.settings["NETWORK"]["photos"])
        order_number = order_id.strip().lstrip("#")
        normalized_order_id = f"#{order_number}"
        order_pattern = re.compile(
            rf"(?<!\d)#?{re.escape(order_number)}(?!\d)",
            re.IGNORECASE,
        )
        image_extensions = {
            ".bmp",
            ".heic",
            ".jpeg",
            ".jfif",
            ".jpg",
            ".png",
            ".tif",
            ".tiff",
            ".webp",
        }
        matched = 0
        copied = 0

        def raise_walk_error(error: OSError) -> None:
            raise error

        try:
            for folder, _subfolders, filenames in os.walk(
                network_folder,
                onerror=raise_walk_error,
            ):
                for filename in filenames:
                    source = Path(folder) / filename
                    if (
                        source.suffix.casefold() not in image_extensions
                        or order_pattern.search(filename) is None
                    ):
                        continue
                    matched += 1
                    try:
                        shutil.copy2(source, self.desktop / source.name)
                        copied += 1
                    except Exception as error:
                        print(f"! Cannot copy photo {source.name}: {error}")
        except Exception as error:
            print(f"! Cannot search photos in {network_folder}: {error}")
            return 0
        if copied:
            print(f"* Photos copied for {normalized_order_id}: {copied}")
        elif matched:
            print(
                f"! {matched} matching photo(s) found for "
                f"{normalized_order_id}, but none could be copied"
            )
        else:
            print(
                f"! No photos found for {normalized_order_id} "
                f"in {network_folder}"
            )
        return copied

    def statistics_path(self) -> Path:
        today = datetime.now().strftime("%d.%m.%Y")
        return Path(self.settings["NETWORK"]["statistics"]) / f"{today}.txt"

    def read_statistics_lines(self, *, keepends: bool = False) -> list[str] | None:
        """Read today's statistics and expose the real network/filesystem error."""
        stat_file = self.statistics_path()
        try:
            return stat_file.read_text(encoding="utf-8").splitlines(
                keepends=keepends
            )
        except FileNotFoundError as error:
            print(f"! Statistics file is unavailable: {stat_file}: {error}")
        except PermissionError as error:
            print(f"! Access denied to statistics file: {stat_file}: {error}")
        except OSError as error:
            print(f"! Cannot open statistics file: {stat_file}: {error}")
        return None

    @staticmethod
    def print_completion_summary(
        delivery: str,
        completed: int,
        incomplete: list[str],
        cancelled: list[str],
    ) -> None:
        """Print exactly one coloured completion line for one delivery type."""
        console = Console()
        if incomplete:
            active_total = completed + len(incomplete)
            message = (
                f"! {delivery} ({completed}/{active_total} completed): "
                f"incomplete orders: {', '.join(incomplete)}."
            )
            colour = "bold red"
        else:
            message = (
                f"✓ {delivery} ({completed} completed): "
                "all orders are completed."
            )
            colour = "bold green"

        if cancelled:
            message += f" Cancelled orders: {', '.join(cancelled)}."
        console.print(f"[{colour}]{message}[/]")

    def check_orders_completion(self) -> None:
        """Check today's UPS, Packeta and Postal orders without modifying them."""
        lines = self.read_statistics_lines()
        if lines is None:
            return

        results = {
            "UPS": {"completed": [], "incomplete": [], "cancelled": []},
            "Packeta": {"completed": [], "incomplete": [], "cancelled": []},
            "Postal": {"completed": [], "incomplete": [], "cancelled": []},
        }

        for line in lines:
            order_match = STATISTICS_ANY_ORDER_PATTERN.search(line)
            delivery = statistics_delivery_type(line)
            if order_match is None or delivery is None:
                continue

            order_id = order_match.group(0)
            status = results[delivery]
            if re.search(r"(?<!\w)Cancelled(?!\w)", line, re.IGNORECASE):
                if order_id not in status["cancelled"]:
                    status["cancelled"].append(order_id)
                continue

            status_suffix = line[order_match.end():]
            assembled, packed = statistics_order_progress(status_suffix)
            if assembled and packed:
                if order_id not in status["completed"]:
                    status["completed"].append(order_id)
            elif order_id not in status["incomplete"]:
                status["incomplete"].append(order_id)

        display_names = {
            "UPS": "UPS",
            "Packeta": "Zásilkovna (Packeta)",
            "Postal": "Postal",
        }
        for delivery in ("UPS", "Packeta", "Postal"):
            status = results[delivery]
            self.print_completion_summary(
                display_names[delivery],
                len(status["completed"]),
                status["incomplete"],
                status["cancelled"],
            )

    def get_greenpo_order_status(self, order_id: str) -> str:
        lines = self.read_statistics_lines()
        if lines is None:
            return "not_found"
        try:
            for line in lines:
                status_suffix = statistics_order_suffix(line, order_id)
                if status_suffix is None:
                    continue
                cancelled = "cancelled" in status_suffix.casefold()
                completed, _packed = statistics_order_progress(status_suffix)
                if cancelled and completed:
                    return "completed_cancelled"
                if cancelled:
                    return "cancelled"
                return "completed" if completed else "active"
        except Exception as error:
            print(f"! Cannot read order status: {error}")
        return "not_found"

    def update_greenpo_statistics(self, order_id: str) -> bool:
        stat_file = self.statistics_path()
        lines = self.read_statistics_lines(keepends=True)
        if lines is None:
            return False
        try:
            found = False
            output: list[str] = []
            for line in lines:
                stripped = line.rstrip("\r\n")
                status_suffix = statistics_order_suffix(stripped, order_id)
                if status_suffix is not None:
                    found = True
                    if "cancelled" not in status_suffix.casefold():
                        stripped += " Cancelled"
                    line = stripped + "\n"
                output.append(line)
            if found:
                stat_file.write_text("".join(output), encoding="utf-8")
            return found
        except Exception as error:
            print(f"! Cannot update statistics: {error}")
            return False

    @staticmethod
    def print_order_progress_warnings(
        order_id: str,
        part_number: int | None,
        order_number: int,
        *,
        assembled: bool,
        packed: bool,
    ) -> None:
        """Print the shared assembled/packed warnings without changing statistics."""
        location_text = statistics_order_location_text(part_number, order_number)
        status_console = Console()
        if assembled:
            status_console.print(
                f"[bold yellow]! Order {order_id} is already assembled! "
                f"{location_text}[/]"
            )
        if packed:
            status_console.print(
                f"[bold red]! Order {order_id} is already packed! "
                f"{location_text}[/]"
            )

    def report_order_progress(self, order_ids: list[str]) -> None:
        """Report existing assembly/packing state without modifying statistics."""
        try:
            with self.statistics_lock:
                lines = self.read_statistics_lines(keepends=True)
            if lines is None:
                return

            for order_id in order_ids:
                location = locate_statistics_order(lines, order_id)
                if location is None:
                    print(f"! Order not found in today's statistics: {order_id}")
                    continue

                (
                    _line_index,
                    _line_number,
                    part_number,
                    order_number,
                    stripped,
                    order_match,
                ) = location
                status_suffix = stripped[order_match.end():]
                assembled, packed = statistics_order_progress(status_suffix)
                self.print_order_progress_warnings(
                    order_id,
                    part_number,
                    order_number,
                    assembled=assembled,
                    packed=packed,
                )
        except Exception as error:
            print(f"! Cannot read order progress: {error}")

    def cancel_order_greenpo_manual(self, order_id: str) -> bool:
        stat_file = self.statistics_path()
        try:
            with self.statistics_lock:
                lines = self.read_statistics_lines(keepends=True)
                if lines is None:
                    return False
                location = locate_statistics_order(lines, order_id)
                if location is None:
                    print(f"! Active order not found: {order_id}")
                    return False

                (
                    line_index,
                    _line_number,
                    part_number,
                    order_number,
                    stripped,
                    order_match,
                ) = location
                status_suffix = stripped[order_match.end():]
                already_cancelled = "cancelled" in status_suffix.casefold()
                assembled, packed = statistics_order_progress(status_suffix)

                if not already_cancelled:
                    original_line = lines[line_index]
                    newline = (
                        "\r\n"
                        if original_line.endswith("\r\n")
                        else "\n" if original_line.endswith("\n") else ""
                    )
                    lines[line_index] = f"{stripped} Cancelled{newline}"
                    stat_file.write_text("".join(lines), encoding="utf-8")

            status_console = Console()

            if not already_cancelled:
                if not assembled:
                    status_console.print(
                        f"[bold green]✓ Order {order_id} cancelled successfully.[/]"
                    )
                else:
                    self.print_order_progress_warnings(
                        order_id,
                        part_number,
                        order_number,
                        assembled=assembled,
                        packed=packed,
                    )
                return True

            removal_question = (
                f"! Order {order_id} is already cancelled. "
                "Remove cancellation? Enter y or n:"
            )
            if threading.current_thread() is threading.main_thread():
                status_console.print(f"[bold yellow]{removal_question}[/]")
                try:
                    answer = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
            else:
                answer = self.console_input.ask(removal_question) or ""

            if answer.casefold() not in {"y", "yes", "у"}:
                print(f"* Cancellation kept for order {order_id}")
                return True

            with self.statistics_lock:
                lines = self.read_statistics_lines(keepends=True)
                if lines is None:
                    return False
                fresh_location = locate_statistics_order(lines, order_id)
                if fresh_location is None:
                    print(f"! Order not found while removing cancellation: {order_id}")
                    return False

                line_index, _, _, _, stripped, order_match = fresh_location
                status_suffix = stripped[order_match.end():]
                cleaned_suffix = re.sub(
                    r"(?<!\S)Cancelled(?=\s|$)",
                    "",
                    status_suffix,
                    count=1,
                    flags=re.IGNORECASE,
                )
                cleaned_suffix = " ".join(cleaned_suffix.split())
                cleaned_suffix = f" {cleaned_suffix}" if cleaned_suffix else ""
                original_line = lines[line_index]
                newline = (
                    "\r\n"
                    if original_line.endswith("\r\n")
                    else "\n" if original_line.endswith("\n") else ""
                )
                lines[line_index] = (
                    f"{stripped[:order_match.end()]}{cleaned_suffix}{newline}"
                )
                stat_file.write_text("".join(lines), encoding="utf-8")

            status_console.print(
                f"[bold green]✓ Cancellation removed from order {order_id}.[/]"
            )
            return True
        except Exception as error:
            print(f"! Cannot update cancellation for {order_id}: {error}")
            return False

    @staticmethod
    def contains_ups_access_point(text: str) -> bool:
        normalized = " ".join(text.casefold().split())
        spellings = ("ups access point", "ups acces point")
        return any(
            spelling in normalized
            or fuzz.partial_ratio(spelling, normalized) >= 90
            for spelling in spellings
        )

    def notify_ups_access_point(self, order_number: str) -> None:
        Console().print(
            f"[bold cyan]! UPS Access Point detected for order {order_number}[/]"
        )
        self.play_sound("ups")

    def extract_order_numbers(
        self, pdf_path: Path, *, notify_access_points: bool = False
    ) -> tuple[list[list], bool, list[str]]:
        delivery_groups = {"UPS": [], "Zasilkovna": [], "Postal": []}
        access_point_found = False
        seen_orders: set[str] = set()
        seen_order_numbers: set[str] = set()
        ordered_order_numbers: list[str] = []
        pattern = re.compile(
            r"(?m)^(#\d+)(?=[^#]*☐).*?type( STEALTH)?\s*\n(.*?)\s*☐"
            r".*?(UPS|Zasilkovna|Postal)(.*?)(?=^#\d+|\Z)",
            re.DOTALL,
        )
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    for order_number in re.findall(r"(?<![\w#])#\d+(?!\d)", text):
                        if order_number not in seen_order_numbers:
                            seen_order_numbers.add(order_number)
                            ordered_order_numbers.append(order_number)
                    for order_number, stealth, name, delivery, tail in pattern.findall(text):
                        if order_number in seen_orders:
                            continue
                        seen_orders.add(order_number)
                        if self.contains_ups_access_point(tail):
                            access_point_found = True
                            if notify_access_points:
                                self.notify_ups_access_point(order_number)
                        delivery_groups[delivery].append([order_number, name.strip(), bool(stealth)])
        except Exception as error:
            print(f"! Cannot process PDF: {error}")
            return [], False, []
        orders = delivery_groups["UPS"] + delivery_groups["Zasilkovna"] + delivery_groups["Postal"]
        return orders, access_point_found, ordered_order_numbers

    def get_connected_printer(self):
        printer_name = self.settings["PRINTER"]["name"]
        for printer in dsdk.get_printers():
            if printer.is_connected and printer_name.casefold() in printer.name.casefold():
                return printer
        raise RuntimeError(f"Connected DYMO printer not found: {printer_name}")

    def print_orders_chunks(self, orders: list[list]) -> None:
        orders = list(reversed(orders))
        if not orders:
            print("! No orders to print")
            return
        chunk_count = 1
        if len(orders) > 100:
            answer = self.console_input.ask(
                f"{len(orders)} orders found. Enter the number of print parts:"
            )
            if answer is None:
                return
            try:
                chunk_count = int(answer)
                if chunk_count < 1:
                    raise ValueError
            except ValueError:
                print("! Invalid number of parts; printing as one part")
                chunk_count = 1

        printer = self.get_connected_printer()
        chunk_size = math.ceil(len(orders) / chunk_count)
        for index in range(chunk_count):
            chunk = orders[index * chunk_size:(index + 1) * chunk_size]
            if not chunk:
                break
            if index:
                if self.console_input.ask("Press Enter to print the next part") is None:
                    return
            self.print_batch(chunk, printer)
        print("* Printing completed")

    def print_batch(self, orders: list[list], printer) -> None:
        console = Console()
        printer_settings = self.settings["PRINTER"]
        with Progress(
            TextColumn("[bold green]Printing labels"),
            BarColumn(bar_width=max(10, console.width // 4), complete_style="green"),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("print", total=len(orders))
            for order_number, name, stealth in orders:
                try:
                    label_key = "stealth_label" if stealth else "order_label"
                    label = dsdk.DymoLabel(filepath=str(configured_path(printer_settings[label_key])))
                    barcode = label.get_label_object("BARCODE")
                    if barcode:
                        barcode.update_data(order_number)
                    name_object = label.get_label_object("NAME")
                    if name_object:
                        name_object.update_data(name)
                    printer.print_label(
                        label,
                        roll_selected=int(printer_settings.get("roll", 2)),
                        barcode_graphics_quality=True,
                    )
                    time.sleep(float(printer_settings.get("delay", 1)))
                except Exception as error:
                    console.print(f"[red]! Print error for {order_number}: {error}")
                progress.update(task, advance=1)

    def process_order_pdf(self, pdf_path: Path) -> None:
        with self.print_lock:
            print(f"* Processing orders: {pdf_path.name}")
            orders, _, _ = self.extract_order_numbers(pdf_path)
            if not orders:
                print("! No order numbers found")
                return
            print(f"* Orders found: {len(orders)}")
            try:
                self.print_orders_chunks(orders)
            except Exception as error:
                print(f"! Printing failed: {error}")

    @staticmethod
    def normalize_scan(data: str) -> str:
        return data.strip().replace("\r", "").replace("\n", "")

    @staticmethod
    def resolve_worker(code: str, workers: dict) -> str | None:
        lowered = code.casefold()
        return next((name for name, value in workers.items() if str(value).casefold() in lowered), None)

    @staticmethod
    def resolve_code(code: str, mapping: dict) -> tuple[str | None, str | None]:
        lowered = code.casefold()
        best_key: tuple[int, str] | None = None
        best_value: tuple[int, str] | None = None
        for key, configured_values in mapping.items():
            key_lower = key.casefold()
            if re.match(rf"^{re.escape(key_lower)}([\s_\-:]|$)", lowered):
                if best_key is None or len(key_lower) > best_key[0]:
                    best_key = (len(key_lower), key)
            values = [configured_values] if isinstance(configured_values, str) else configured_values
            for value in values:
                value_lower = value.casefold()
                if value_lower in lowered and (best_value is None or len(value_lower) > best_value[0]):
                    best_value = (len(value_lower), key)
        if best_key:
            return best_key[1], "key"
        return (best_value[1], "value") if best_value else (None, None)

    def com_listener(self) -> None:
        configuration = self.settings["COM"]
        port = configuration["port"]
        baudrate = int(configuration.get("baudrate", 9600))
        first_pair: tuple[str, str] | None = None
        worker_name: str | None = None
        while not self.stop_event.is_set():
            try:
                self.serial_connection = serial.Serial(port, baudrate, timeout=0.2)
                print(f"* Listening to scanner on {port}")
                while not self.stop_event.is_set():
                    raw = self.serial_connection.readline()
                    if not raw:
                        continue
                    scanned = self.normalize_scan(raw.decode(errors="ignore"))
                    if not scanned:
                        continue
                    worker = self.resolve_worker(scanned, self.workers)
                    if worker:
                        worker_name = worker
                        print(f"* Worker: {worker_name}")
                        continue
                    product = self.resolve_code(scanned, self.settings["ORDERS"])
                    if not product[0]:
                        print("! Unknown product code")
                        self.play_sound("error")
                        continue
                    if first_pair is None:
                        first_pair = product
                        continue
                    product_ok = first_pair[0] == product[0] and first_pair[1] != product[1]
                    if product_ok and worker_name:
                        print(f"* ALL GOOD ({worker_name})")
                        self.play_sound("good")
                        self.photo_jobs.put((first_pair[0], worker_name))
                    else:
                        print("! NOT GOOD")
                        self.play_sound("bad")
                    first_pair = None
                    worker_name = None
            except Exception as error:
                if not self.stop_event.is_set():
                    print(f"! COM error on {port}: {error}; retrying in 5 seconds")
                    self.stop_event.wait(5)
            finally:
                if self.serial_connection:
                    try:
                        self.serial_connection.close()
                    except Exception:
                        pass
                    self.serial_connection = None

    def find_camera_id(self) -> int:
        requested_name = self.settings["CAMERA"]["name"]
        names = FilterGraph().get_input_devices()
        for camera_id, camera_name in enumerate(names):
            print(f"* Camera found: {camera_name} (OpenCV ID {camera_id})")
            if camera_name.casefold() == requested_name.casefold():
                return camera_id
        available = ", ".join(names) if names else "none"
        raise RuntimeError(f"Camera '{requested_name}' not found. Available: {available}")

    def open_camera(self) -> cv2.VideoCapture:
        camera_id = self.find_camera_id()
        configuration = self.settings["CAMERA"]
        camera = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
        camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("M", "J", "P", "G"))
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, int(configuration.get("width", 4656)))
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, int(configuration.get("height", 3496)))
        camera.set(cv2.CAP_PROP_FOCUS, float(configuration.get("focus", 360)))
        if not camera.isOpened():
            camera.release()
            raise RuntimeError(f"Cannot open camera '{configuration['name']}'")
        time.sleep(float(configuration.get("warmup_seconds", 2)))
        print(f"* Camera opened: {configuration['name']} (OpenCV ID {camera_id})")
        return camera

    def camera_worker(self) -> None:
        idle_since: float | None = None
        idle_timeout = float(self.settings["CAMERA"].get("idle_timeout_seconds", 10))
        while not self.stop_event.is_set():
            try:
                product_key, worker_name = self.photo_jobs.get(timeout=1)
            except queue.Empty:
                if self.camera is not None and idle_since and time.monotonic() - idle_since >= idle_timeout:
                    self.camera.release()
                    self.camera = None
                    idle_since = None
                    print("* Camera closed after inactivity")
                continue
            try:
                if self.camera is None:
                    self.camera = self.open_camera()
                time.sleep(float(self.settings["CAMERA"].get("photo_delay_seconds", 2)))
                frames = []
                for _ in range(5):
                    ok, frame = self.camera.read()
                    if ok:
                        frames.append(frame)
                    time.sleep(0.1)
                if len(frames) < 3:
                    raise RuntimeError("Camera did not return enough frames")
                now = datetime.now()
                filename = f"{product_key}_{now.strftime('%d.%m_%H-%M-%S')}.jpg"
                destination = self.photo_folder / filename
                if not cv2.imwrite(str(destination), frames[2]):
                    raise RuntimeError(f"Cannot write photo: {destination}")
                self.write_worker_log(worker_name, product_key)
                print(f"* Photo saved: {filename}")
                self.play_sound("good")
                idle_since = time.monotonic()
            except Exception as error:
                print(f"! Camera error: {error}")
                if self.camera is not None:
                    self.camera.release()
                    self.camera = None
            finally:
                self.photo_jobs.task_done()

    def write_worker_log(self, worker_name: str, product_key: str) -> None:
        log_file = self.logs_folder / f"{worker_name}.txt"
        lines = log_file.read_text(encoding="utf-8").splitlines(keepends=True) if log_file.exists() else []
        entries = lines[1:] if lines else []
        entries.append(f"{product_key}   {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n")
        log_file.write_text(f"========== Count: {len(entries)} ==========\n" + "".join(entries), encoding="utf-8")

    def cleanup_old_photos(self, older_months: int = 12) -> None:
        cutoff = time.time() - older_months * 30 * 24 * 60 * 60
        removed = 0
        for file in self.photo_folder.iterdir():
            if file.is_file() and file.stat().st_mtime < cutoff:
                send2trash(str(file))
                removed += 1
        if removed:
            print(f"* Old photos moved to Recycle Bin: {removed}")

    def shutdown(self) -> None:
        if self.stop_event.is_set() and self.observer is None:
            return
        self.stop_event.set()
        print("* Stopping Admin...")
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        if self.serial_connection is not None:
            try:
                self.serial_connection.close()
            except Exception:
                pass
        if self.camera is not None:
            self.camera.release()
            self.camera = None
        try:
            pygame.mixer.quit()
        except Exception:
            pass


def wait_until_file_is_ready(file_path: Path, timeout: float = 60, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    previous_size: int | None = None
    stable_checks = 0
    while time.monotonic() < deadline:
        try:
            current_size = file_path.stat().st_size
            with file_path.open("rb"):
                pass
            if current_size > 0 and current_size == previous_size:
                stable_checks += 1
                if stable_checks >= 2:
                    return True
            else:
                stable_checks = 0
            previous_size = current_size
        except (FileNotFoundError, PermissionError, OSError):
            stable_checks = 0
        time.sleep(interval)
    return False


def find_latest_order_pdf(downloads: Path) -> Path | None:
    today = datetime.now().strftime("%d.%m.%Y")
    pattern = re.compile(rf"^{re.escape(today)} Part (\d+)\.pdf$", re.IGNORECASE)
    candidates = []
    for file in downloads.iterdir():
        match = pattern.fullmatch(file.name) if file.is_file() else None
        if match:
            candidates.append((int(match.group(1)), file))
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def current_label_path(downloads: Path) -> Path | None:
    order_pdf = find_latest_order_pdf(downloads)
    return order_pdf.with_name(f"{order_pdf.stem} (Label).pdf") if order_pdf else None


def read_pdf_without_leading_junk(path: Path) -> PdfReader:
    """Read a PDF after removing bytes placed before its required %PDF- header."""
    data = path.read_bytes()
    header_position = data.find(b"%PDF-")
    if header_position < 0:
        raise ValueError(f"PDF header was not found in {path.name}")
    return PdfReader(io.BytesIO(data[header_position:]), strict=False)


class PDFHandler(FileSystemEventHandler):
    def __init__(self, app: AdminApp):
        self.app = app
        self.processing_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.pending_paths: set[str] = set()
        self.recent_paths: dict[str, float] = {}
        self.duplicate_event_window = 5.0

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._dispatch(Path(event.src_path))

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._dispatch(Path(event.dest_path))

    def _dispatch(self, file_path: Path) -> None:
        name = file_path.name.casefold()
        if name not in {
            "mpdf.pdf",
            "qwe.pdf",
            "qwez.pdf",
            "change.pdf",
        } and CHANGE_LABEL_SOURCE_PATTERN.fullmatch(file_path.name) is None:
            return

        # A browser/download manager can produce both created and moved events
        # for the same completed file. Without deduplication, the second worker
        # waits 60 seconds for a source path that the first worker already renamed.
        path_key = os.path.normcase(str(file_path.resolve(strict=False)))
        now = time.monotonic()
        with self.event_lock:
            self.recent_paths = {
                key: finished_at
                for key, finished_at in self.recent_paths.items()
                if now - finished_at < self.duplicate_event_window
            }
            if path_key in self.pending_paths or path_key in self.recent_paths:
                return
            self.pending_paths.add(path_key)

        threading.Thread(
            target=self._process_once,
            args=(file_path, path_key),
            daemon=True,
        ).start()

    def _process_once(self, file_path: Path, path_key: str) -> None:
        try:
            self._process(file_path)
        finally:
            with self.event_lock:
                self.pending_paths.discard(path_key)
                self.recent_paths[path_key] = time.monotonic()

    def _process(self, file_path: Path) -> None:
        with self.processing_lock:
            name = file_path.name.casefold()
            change_label_match = CHANGE_LABEL_SOURCE_PATTERN.fullmatch(
                file_path.name
            )
            print(f"* Download detected: {file_path.name}")
            if not wait_until_file_is_ready(file_path):
                print(f"! Download did not become ready: {file_path.name}")
                return
            try:
                if name == "mpdf.pdf":
                    renamed = self.rename_order_pdf(file_path)
                    self.app.extract_order_numbers(
                        renamed, notify_access_points=True
                    )
                    self.app.send_pdf(renamed)
                    part_number = renamed.stem.rsplit(" ", 1)[-1]
                    print(f"* Printing is waiting for command: print part {part_number}")
                elif name == "change.pdf":
                    _, _, order_numbers = self.app.extract_order_numbers(
                        file_path, notify_access_points=True
                    )
                    if not order_numbers:
                        raise ValueError(
                            "No order numbers were found in change.pdf"
                        )
                    renamed = self.rename_change_pdf(file_path, order_numbers)
                    self.app.report_order_progress(order_numbers)
                    self.app.send_pdf(renamed)
                elif change_label_match is not None:
                    order_numbers = [
                        f"#{number.strip()}"
                        for number in change_label_match.group(1).split(",")
                    ]
                    renamed = self.rename_change_label_pdf(
                        file_path, order_numbers
                    )
                    self.app.report_order_progress(order_numbers)
                    self.app.send_pdf(renamed)
                elif name == "qwe.pdf":
                    self.rename_label_pdf(file_path)
                else:
                    self.append_label_fragment(file_path)
            except Exception as error:
                print(f"! Cannot process {file_path.name}: {error}")

    def rename_order_pdf(self, source: Path) -> Path:
        today = datetime.now().strftime("%d.%m.%Y")
        pattern = re.compile(rf"^{re.escape(today)} Part (\d+)\.pdf$", re.IGNORECASE)
        parts = []
        for file in self.app.downloads.iterdir():
            match = pattern.fullmatch(file.name)
            if match:
                parts.append(int(match.group(1)))
        destination = self.app.downloads / f"{today} Part {max(parts, default=0) + 1}.pdf"
        os.replace(source, destination)
        print(f"* Order PDF renamed: {destination.name}")
        return destination

    def rename_change_pdf(self, source: Path, order_numbers: list[str]) -> Path:
        today = datetime.now().strftime("%d.%m.%Y")
        joined_orders = ", ".join(order_numbers)
        destination = self.app.downloads / (
            f"Change {joined_orders} ({today}).pdf"
        )
        os.replace(source, destination)
        print(f"* Change PDF renamed: {destination.name}")
        return destination

    def rename_change_label_pdf(
        self, source: Path, order_numbers: list[str]
    ) -> Path:
        today = datetime.now().strftime("%d.%m.%Y")
        joined_orders = ", ".join(order_numbers)
        destination = self.app.downloads / (
            f"Change Label {joined_orders} ({today}).pdf"
        )
        os.replace(source, destination)
        print(f"* Change Label PDF renamed: {destination.name}")
        return destination

    def rename_label_pdf(self, source: Path) -> Path:
        destination = current_label_path(self.app.downloads)
        if destination is None:
            raise FileNotFoundError("Today's order PDF was not found")
        os.replace(source, destination)
        print(f"* Label PDF renamed: {destination.name}")
        print(f"* Sending is waiting for command: send part {destination.stem.split(' Part ')[-1].split(' ')[0]}")
        return destination

    def append_label_fragment(self, fragment: Path) -> Path:
        destination = current_label_path(self.app.downloads)
        if destination is None or not destination.exists():
            raise FileNotFoundError("Current Label PDF was not found; download qwe.pdf first")
        temporary = destination.with_name(f".{destination.stem}.merging.pdf")
        writer = PdfWriter()
        try:
            for source in (destination, fragment):
                reader = read_pdf_without_leading_junk(source)
                for page in reader.pages:
                    writer.add_page(page)
            with temporary.open("wb") as output:
                writer.write(output)
            os.replace(temporary, destination)
            fragment.unlink()
        finally:
            writer.close()
            if temporary.exists():
                temporary.unlink()
        print(f"* Label fragment appended: {destination.name}")
        print(f"* Sending is waiting for command: send part {destination.stem.split(' Part ')[-1].split(' ')[0]}")
        return destination


def acquire_single_instance_mutex():
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        ctypes.windll.user32.MessageBoxW(0, "Admin is already running", "Admin", 0x10)
        raise SystemExit(1)
    return mutex


def main() -> None:
    mutex = acquire_single_instance_mutex()
    app: AdminApp | None = None
    try:
        check_for_updates()
        ensure_companion_files()
        app = AdminApp(synchronize_settings())

        def stop_handler(*_args) -> None:
            app.stop_event.set()

        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)
        win32api.SetConsoleCtrlHandler(lambda _event: (app.stop_event.set() or True), True)
        if datetime.now().day == 5:
            app.cleanup_old_photos(12)
        app.start()
    except Exception as error:
        print(f"FATAL: {error}")
        if app:
            app.shutdown()
        input("Press Enter to close...")
        raise
    finally:
        mutex = None


if __name__ == "__main__":
    main()
