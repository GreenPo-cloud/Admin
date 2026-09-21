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
    ("pypdf", "pypdf"),
    ("pygame", "pygame"),
    ("pygrabber", "pygrabber"),
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
CURRENT_VERSION = "2.6"
VERSION_URL = "https://raw.githubusercontent.com/GreenPo-cloud/Admin/main/version.txt"
PYTHON_URL = "https://raw.githubusercontent.com/GreenPo-cloud/Admin/main/Admin.py"
READ_PUSH_URL = "https://raw.githubusercontent.com/GreenPo-cloud/Admin/main/ReadPush.py"
MUTEX_NAME = "GreenPoAdminProgramMutex"


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


def configured_path(value: str) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def check_for_updates() -> None:
    """Check once at startup and install only a strictly newer version."""
    try:
        response = requests.get(VERSION_URL, timeout=5)
        response.raise_for_status()
        latest = response.text.strip()
        current_key = version_key(CURRENT_VERSION)
        latest_key = version_key(latest)
        if not current_key or not latest_key or latest_key <= current_key:
            print(f"* Admin {CURRENT_VERSION} is up to date")
            return

        print(f"* Admin update found: {latest}")
        update_program()
    except Exception as error:
        print(f"! Update check failed: {error}")


def update_program() -> None:
    response = requests.get(PYTHON_URL, timeout=15)
    response.raise_for_status()
    read_push_response = requests.get(READ_PUSH_URL, timeout=15)
    read_push_response.raise_for_status()
    current_file = Path(__file__).resolve()
    temporary_file = current_file.with_suffix(".py.new")
    read_push_file = current_file.with_name("ReadPush.py")
    temporary_read_push = read_push_file.with_suffix(".py.new")
    batch_file = current_file.with_suffix(".update.bat")
    temporary_file.write_text(response.text, encoding="utf-8")
    temporary_read_push.write_text(read_push_response.text, encoding="utf-8")
    batch_file.write_text(
        "@echo off\n"
        "timeout /t 2 >nul\n"
        f'move /Y "{temporary_file}" "{current_file}" >nul\n'
        f'move /Y "{temporary_read_push}" "{read_push_file}" >nul\n'
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
        self._request: tuple[threading.Event, list[str]] | None = None

    def ask(self, prompt: str) -> str | None:
        completed = threading.Event()
        answer: list[str] = []
        with self._lock:
            if self._request is not None:
                raise RuntimeError("Another console question is already active")
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
        self.serial_connection: serial.Serial | None = None
        self.camera: cv2.VideoCapture | None = None
        self.observer: Observer | None = None
        self.threads: list[threading.Thread] = []
        self.print_lock = threading.Lock()
        self.audio: dict[str, pygame.mixer.Sound] = {}

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
        self.start_push_listener()
        print("* Watching Downloads for mpdf.pdf, qwe.pdf and qwez.pdf")
        print("* Commands: print part N | cancel #1234567 | copy #1234567 | help | exit")
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
            thread = start_slack_notification_listener(self.stop_event)
            if thread is not None:
                self.threads.append(thread)
        except Exception as error:
            print(f"! Slack push listener is disabled: {error}")

    def _start_thread(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

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
            print("cancel #1234567  - mark an order Cancelled")
            print("copy #1234567    - copy matching photos to Desktop")
            print("exit             - stop Admin")
            return

        print_match = re.fullmatch(r"print\s+part\s+(\d+)", command, re.IGNORECASE)
        if print_match:
            part_number = int(print_match.group(1))
            if part_number < 1:
                print("! Part number must be 1 or greater")
                return
            self.print_part_manual(part_number)
            return

        match = re.fullmatch(r"(cancel|copy)\s+#?(\d+)", command, re.IGNORECASE)
        if not match:
            print("! Unknown command. Type: help")
            return
        action, number = match.groups()
        order_id = f"#{number}"
        if action.casefold() == "cancel":
            self.cancel_order_greenpo_manual(order_id)
        else:
            self.copy_photo_from_network(order_id)

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

    def copy_with_retry(self, file_path: Path) -> None:
        network_folder = Path(self.settings["NETWORK"]["downloads"])
        destination = network_folder / file_path.name
        while not self.stop_event.is_set():
            try:
                shutil.copy2(file_path, destination)
                print(f"* Copied to second computer: {destination}")
                return
            except Exception as error:
                print(f"! Copy failed: {error}; retrying in 15 seconds")
                self.stop_event.wait(15)

    def send_pdf(self, file_path: Path) -> None:
        # One worker preserves update order when qwe.pdf is immediately followed
        # by qwez.pdf. Parallel copies could otherwise restore an older remote PDF.
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
        order_pattern = re.compile(rf"{re.escape(order_id)}(?=\D|$)", re.IGNORECASE)
        copied = 0
        try:
            for source in network_folder.iterdir():
                if (
                    source.is_file()
                    and source.suffix.casefold() in {".jpg", ".jpeg", ".png"}
                    and order_pattern.search(source.name)
                ):
                    shutil.copy2(source, self.desktop / source.name)
                    copied += 1
        except Exception as error:
            print(f"! Cannot copy photos: {error}")
            return 0
        print(f"* Photos copied for {order_id}: {copied}" if copied else f"! No photos found for {order_id}")
        return copied

    def statistics_path(self) -> Path:
        today = datetime.now().strftime("%d.%m.%Y")
        return Path(self.settings["NETWORK"]["statistics"]) / f"{today}.txt"

    def get_greenpo_order_status(self, order_id: str) -> str:
        stat_file = self.statistics_path()
        if not stat_file.exists():
            return "not_found"
        try:
            order_pattern = re.compile(rf"^{re.escape(order_id)}(?=\D|$)")
            for line in stat_file.read_text(encoding="utf-8").splitlines():
                if not order_pattern.match(line):
                    continue
                cancelled = "Cancelled" in line
                completed = "+" in line or "(+)" in line
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
        if not stat_file.exists():
            return False
        try:
            lines = stat_file.read_text(encoding="utf-8").splitlines(keepends=True)
            found = False
            output: list[str] = []
            order_pattern = re.compile(rf"^{re.escape(order_id)}(?=\D|$)")
            for line in lines:
                stripped = line.rstrip("\r\n")
                if order_pattern.match(stripped):
                    found = True
                    if "Cancelled" not in stripped:
                        stripped += " Cancelled"
                    line = stripped + "\n"
                output.append(line)
            if found:
                stat_file.write_text("".join(output), encoding="utf-8")
            return found
        except Exception as error:
            print(f"! Cannot update statistics: {error}")
            return False

    def cancel_order_greenpo_manual(self, order_id: str) -> bool:
        status = self.get_greenpo_order_status(order_id)
        if not self.update_greenpo_statistics(order_id):
            print(f"! Active order not found: {order_id}")
            return False
        if status in {"completed", "completed_cancelled"}:
            print(f"! COMPLETED ORDER CANCELLED: {order_id}")
            print("! Find this order among completed orders")
        elif status == "cancelled":
            print(f"* Order was already cancelled: {order_id}")
        else:
            print(f"* ORDER CANCELLED: {order_id}")
        return True

    def extract_order_numbers(self, pdf_path: Path) -> tuple[list[list], bool]:
        delivery_groups = {"UPS": [], "Zasilkovna": [], "Postal": []}
        access_point_found = False
        seen_orders: set[str] = set()
        pattern = re.compile(
            r"(?m)^(#\d+)(?=[^#]*☐).*?type( STEALTH)?\s*\n(.*?)\s*☐"
            r".*?(UPS|Zasilkovna|Postal)(.*?)(?=^#\d+|\Z)",
            re.DOTALL,
        )
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    for order_number, stealth, name, delivery, tail in pattern.findall(text):
                        if order_number in seen_orders:
                            continue
                        seen_orders.add(order_number)
                        if fuzz.partial_ratio("ups access point", tail.casefold()) >= 90:
                            print(f"! UPS Access Point detected: {order_number}")
                            access_point_found = True
                            self.play_sound("ups")
                        delivery_groups[delivery].append([order_number, name.strip(), bool(stealth)])
        except Exception as error:
            print(f"! Cannot process PDF: {error}")
            return [], False
        orders = delivery_groups["UPS"] + delivery_groups["Zasilkovna"] + delivery_groups["Postal"]
        return orders, access_point_found

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
            orders, _ = self.extract_order_numbers(pdf_path)
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
        if file_path.name.casefold() not in {"mpdf.pdf", "qwe.pdf", "qwez.pdf"}:
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
            print(f"* Download detected: {file_path.name}")
            if not wait_until_file_is_ready(file_path):
                print(f"! Download did not become ready: {file_path.name}")
                return
            try:
                if name == "mpdf.pdf":
                    renamed = self.rename_order_pdf(file_path)
                    part_number = renamed.stem.rsplit(" ", 1)[-1]
                    print(f"* Printing is waiting for command: print part {part_number}")
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
        self.app.send_pdf(destination)
        return destination

    def rename_label_pdf(self, source: Path) -> Path:
        destination = current_label_path(self.app.downloads)
        if destination is None:
            raise FileNotFoundError("Today's order PDF was not found")
        os.replace(source, destination)
        print(f"* Label PDF renamed: {destination.name}")
        self.app.send_pdf(destination)
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
        self.app.send_pdf(destination)
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
        app = AdminApp(load_settings())

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
