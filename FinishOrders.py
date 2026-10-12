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
import random
import re
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS_PATH = BASE_DIR / "Admin_settings.json"
MUTEX_NAME = "Local\\GreenPoFinishOrdersMutex"
ERROR_ALREADY_EXISTS = 183
SW_SHOW = 5
SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_C = 0x43
VK_RETURN = 0x0D
VK_F5 = 0x74
CF_UNICODETEXT = 13
USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
ENUM_WINDOWS_CALLBACK = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HWND,
    wintypes.LPARAM,
)
USER32.EnumWindows.argtypes = [ENUM_WINDOWS_CALLBACK, wintypes.LPARAM]
USER32.EnumWindows.restype = wintypes.BOOL
USER32.IsWindowVisible.argtypes = [wintypes.HWND]
USER32.IsWindowVisible.restype = wintypes.BOOL
USER32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetClassNameW.restype = ctypes.c_int
USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
USER32.GetWindowTextLengthW.restype = ctypes.c_int
USER32.GetWindowTextW.argtypes = [
    wintypes.HWND,
    wintypes.LPWSTR,
    ctypes.c_int,
]
USER32.GetWindowTextW.restype = ctypes.c_int
USER32.IsIconic.argtypes = [wintypes.HWND]
USER32.IsIconic.restype = wintypes.BOOL
USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
USER32.ShowWindow.restype = wintypes.BOOL
USER32.BringWindowToTop.argtypes = [wintypes.HWND]
USER32.BringWindowToTop.restype = wintypes.BOOL
USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.GetForegroundWindow.argtypes = []
USER32.GetForegroundWindow.restype = wintypes.HWND
USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
USER32.GetWindowRect.restype = wintypes.BOOL
USER32.GetSystemMetrics.argtypes = [ctypes.c_int]
USER32.GetSystemMetrics.restype = ctypes.c_int
USER32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
USER32.SetCursorPos.restype = wintypes.BOOL
USER32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
USER32.GetCursorPos.restype = wintypes.BOOL
USER32.mouse_event.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
USER32.mouse_event.restype = None
USER32.keybd_event.argtypes = [
    wintypes.BYTE,
    wintypes.BYTE,
    wintypes.DWORD,
    ctypes.c_void_p,
]
USER32.keybd_event.restype = None
USER32.OpenClipboard.argtypes = [wintypes.HWND]
USER32.OpenClipboard.restype = wintypes.BOOL
USER32.CloseClipboard.argtypes = []
USER32.CloseClipboard.restype = wintypes.BOOL
USER32.GetClipboardData.argtypes = [wintypes.UINT]
USER32.GetClipboardData.restype = wintypes.HANDLE
USER32.EnumClipboardFormats.argtypes = [wintypes.UINT]
USER32.EnumClipboardFormats.restype = wintypes.UINT
USER32.GetClipboardFormatNameW.argtypes = [
    wintypes.UINT,
    wintypes.LPWSTR,
    ctypes.c_int,
]
USER32.GetClipboardFormatNameW.restype = ctypes.c_int
USER32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
USER32.RegisterClipboardFormatW.restype = wintypes.UINT
USER32.GetClipboardSequenceNumber.argtypes = []
USER32.GetClipboardSequenceNumber.restype = wintypes.DWORD
KERNEL32.GlobalLock.argtypes = [wintypes.HGLOBAL]
KERNEL32.GlobalLock.restype = wintypes.LPVOID
KERNEL32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
KERNEL32.GlobalUnlock.restype = wintypes.BOOL
KERNEL32.GlobalSize.argtypes = [wintypes.HGLOBAL]
KERNEL32.GlobalSize.restype = ctypes.c_size_t

INTERACTION_TIMING = {
    "mouse_move_min": 0.25,
    "mouse_move_max": 0.75,
    "click_delay_min": 0.2,
    "click_delay_max": 1.0,
    "enter_delay_min": 0.0,
    "enter_delay_max": 1.0,
}
_CANCEL_CHECK = lambda: False


class FinishCycleCancelled(RuntimeError):
    """Raised when the operator releases Space during a finish session."""


def configure_cancel_check(callback) -> None:
    global _CANCEL_CHECK
    _CANCEL_CHECK = callback if callable(callback) else (lambda: False)


def raise_if_cancelled() -> None:
    if _CANCEL_CHECK():
        raise FinishCycleCancelled("stopped by Space")


def cancellable_sleep(seconds: float) -> None:
    """Sleep in short intervals so Space can stop the current sequence."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        raise_if_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.03, remaining))


def configure_interaction_timing(**values: float) -> None:
    """Set bounded timing ranges used by the finishing UI automation."""
    expected = set(INTERACTION_TIMING)
    if set(values) != expected:
        missing = expected - set(values)
        extra = set(values) - expected
        raise ValueError(
            f"Invalid interaction timing keys; missing={missing}, extra={extra}"
        )
    for minimum_name, maximum_name in (
        ("mouse_move_min", "mouse_move_max"),
        ("click_delay_min", "click_delay_max"),
        ("enter_delay_min", "enter_delay_max"),
    ):
        minimum = float(values[minimum_name])
        maximum = float(values[maximum_name])
        if minimum < 0 or maximum < minimum:
            raise ValueError(
                f"Invalid timing range {minimum_name}/{maximum_name}: "
                f"{minimum}..{maximum}"
            )
    INTERACTION_TIMING.update(
        {name: float(value) for name, value in values.items()}
    )


def truncated_normal_seconds(minimum: float, maximum: float) -> float:
    """Return a normally distributed value strictly bounded to one range."""
    if minimum < 0 or maximum < minimum:
        raise ValueError(f"Invalid delay range: {minimum}..{maximum}")
    if minimum == maximum:
        return minimum
    mean = (minimum + maximum) / 2
    standard_deviation = (maximum - minimum) / 6
    while True:
        value = random.gauss(mean, standard_deviation)
        if minimum <= value <= maximum:
            return value


def interaction_delay(kind: str) -> float:
    """Sleep for one configured truncated-normal delay and return its value."""
    minimum = INTERACTION_TIMING[f"{kind}_min"]
    maximum = INTERACTION_TIMING[f"{kind}_max"]
    delay = truncated_normal_seconds(minimum, maximum)
    cancellable_sleep(delay)
    return delay


def random_mouse_move_duration() -> float:
    return truncated_normal_seconds(
        INTERACTION_TIMING["mouse_move_min"],
        INTERACTION_TIMING["mouse_move_max"],
    )


def truncated_normal_pixel(minimum: int, maximum: int) -> int:
    """Choose a bounded integer pixel, favouring the centre of the range."""
    if maximum < minimum:
        raise ValueError(f"Invalid pixel range: {minimum}..{maximum}")
    if minimum == maximum:
        return minimum
    mean = (minimum + maximum) / 2
    standard_deviation = max((maximum - minimum) / 6, 0.5)
    while True:
        pixel = round(random.gauss(mean, standard_deviation))
        if minimum <= pixel <= maximum:
            return pixel


def random_dropdown_point(match: dict, edge_inset: int) -> tuple[int, int]:
    """Choose independent normally distributed X/Y pixels inside Dropdown."""
    return random_pattern_point(match, edge_inset, "Dropdown")


def random_pattern_point(
    match: dict,
    edge_inset: int,
    pattern_name: str,
) -> tuple[int, int]:
    """Choose independent normally distributed X/Y pixels inside a match."""
    if edge_inset < 0:
        raise ValueError("Pattern edge inset cannot be negative")
    left = int(match["left"])
    top = int(match["top"])
    width = int(match["width"])
    height = int(match["height"])
    minimum_x = left + edge_inset
    maximum_x = left + width - 1 - edge_inset
    minimum_y = top + edge_inset
    maximum_y = top + height - 1 - edge_inset
    if maximum_x < minimum_x or maximum_y < minimum_y:
        raise ValueError(
            f"{pattern_name} is too small for a {edge_inset}px edge inset: "
            f"{width}x{height}"
        )
    return (
        truncated_normal_pixel(minimum_x, maximum_x),
        truncated_normal_pixel(minimum_y, maximum_y),
    )


def find_chrome_windows() -> list[tuple[int, str]]:
    """Return visible top-level Chrome windows with non-empty titles."""
    windows: list[tuple[int, str]] = []

    @ENUM_WINDOWS_CALLBACK
    def collect(window_handle: int, _extra: int) -> bool:
        if not USER32.IsWindowVisible(window_handle):
            return True

        class_name = ctypes.create_unicode_buffer(256)
        USER32.GetClassNameW(window_handle, class_name, len(class_name))
        if class_name.value != "Chrome_WidgetWin_1":
            return True

        title_length = USER32.GetWindowTextLengthW(window_handle)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        USER32.GetWindowTextW(
            window_handle,
            title_buffer,
            len(title_buffer),
        )
        title = title_buffer.value.strip()
        if title:
            windows.append((int(window_handle), title))
        return True

    if not USER32.EnumWindows(collect, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return windows


def find_matching_chrome_window(title_fragment: str) -> tuple[int, str]:
    """Return the Chrome window matching one title fragment."""
    requested = title_fragment.casefold().strip()
    windows = find_chrome_windows()
    matches = [
        (window_handle, title)
        for window_handle, title in windows
        if requested and requested in title.casefold()
    ]
    if not matches:
        available = "; ".join(title for _handle, title in windows) or "none"
        raise RuntimeError(
            f"Chrome window containing {title_fragment!r} was not found. "
            f"Visible Chrome windows: {available}"
        )
    return matches[0]


def activate_window(window_handle: int) -> None:
    """Restore one top-level window and move it to the foreground."""
    if USER32.IsIconic(window_handle):
        USER32.ShowWindow(window_handle, SW_RESTORE)
    else:
        USER32.ShowWindow(window_handle, SW_SHOW)
    USER32.BringWindowToTop(window_handle)
    USER32.SetForegroundWindow(window_handle)
    time.sleep(0.4)
    foreground_window = USER32.GetForegroundWindow()
    if not foreground_window or int(foreground_window) != window_handle:
        raise RuntimeError(
            "Windows refused to move the Chrome window to the foreground"
        )


def activate_chrome_window(title_fragment: str) -> str:
    """Restore and activate the Chrome window matching one title fragment."""
    window_handle, title = find_matching_chrome_window(title_fragment)
    activate_window(window_handle)
    return title


def window_rectangle(window_handle: int) -> tuple[int, int, int, int]:
    """Return one window rectangle in absolute screen coordinates."""
    rectangle = wintypes.RECT()
    if not USER32.GetWindowRect(window_handle, ctypes.byref(rectangle)):
        raise ctypes.WinError(ctypes.get_last_error())
    if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
        raise RuntimeError("Chrome returned an invalid window rectangle")
    return (
        rectangle.left,
        rectangle.top,
        rectangle.right,
        rectangle.bottom,
    )


def scan_dropdown_patterns(
    *,
    title_fragment: str,
    pattern_path: str,
    confidence: float,
    capture_path: str | None = None,
    preview_path: str | None = None,
) -> dict:
    """Capture Chrome in memory and locate dropdown templates."""
    import cv2
    import numpy as np
    from PIL import ImageGrab

    raise_if_cancelled()
    if not 0 < confidence <= 1:
        raise ValueError("pattern_confidence must be greater than 0 and at most 1")

    resolved_pattern = configured_path(pattern_path)
    resolved_capture = configured_path(capture_path) if capture_path else None
    resolved_preview = configured_path(preview_path) if preview_path else None
    if not resolved_pattern.is_file():
        raise FileNotFoundError(f"Dropdown pattern was not found: {resolved_pattern}")

    window_handle, title = find_matching_chrome_window(title_fragment)
    activate_window(window_handle)
    left, top, right, bottom = window_rectangle(window_handle)

    screenshot = ImageGrab.grab(
        bbox=(left, top, right, bottom),
        all_screens=True,
    ).convert("RGB")
    raise_if_cancelled()
    if resolved_capture is not None:
        resolved_capture.parent.mkdir(parents=True, exist_ok=True)
        screenshot.save(resolved_capture)

    screenshot_bgr = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_RGB2BGR)
    template = cv2.imread(str(resolved_pattern), cv2.IMREAD_UNCHANGED)
    if template is None:
        raise RuntimeError(f"Cannot read dropdown pattern: {resolved_pattern}")
    if template.ndim == 3 and template.shape[2] == 4:
        template = cv2.cvtColor(template, cv2.COLOR_BGRA2BGR)
    elif template.ndim == 2:
        template = cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)

    template_height, template_width = template.shape[:2]
    screen_height, screen_width = screenshot_bgr.shape[:2]
    if template_width > screen_width or template_height > screen_height:
        raise RuntimeError("Dropdown pattern is larger than the Chrome capture")

    scores = cv2.matchTemplate(
        screenshot_bgr,
        template,
        cv2.TM_CCOEFF_NORMED,
    )
    threshold_mask = (scores >= confidence).astype(np.uint8)
    component_count, labels, _stats, _centroids = (
        cv2.connectedComponentsWithStats(threshold_mask, connectivity=8)
    )

    matches = []
    for label in range(1, component_count):
        y_values, x_values = np.where(labels == label)
        if not len(x_values):
            continue
        best_index = int(np.argmax(scores[y_values, x_values]))
        local_x = int(x_values[best_index])
        local_y = int(y_values[best_index])
        match_confidence = float(scores[local_y, local_x])
        matches.append(
            {
                "left": left + local_x,
                "top": top + local_y,
                "width": template_width,
                "height": template_height,
                "center_x": left + local_x + template_width // 2,
                "center_y": top + local_y + template_height // 2,
                "confidence": match_confidence,
                "local_x": local_x,
                "local_y": local_y,
            }
        )
    matches.sort(key=lambda match: (match["center_y"], match["center_x"]))
    raise_if_cancelled()

    if resolved_preview is not None:
        resolved_preview.parent.mkdir(parents=True, exist_ok=True)
        preview = screenshot_bgr.copy()
        for index, match in enumerate(matches, start=1):
            local_x = match["local_x"]
            local_y = match["local_y"]
            cv2.rectangle(
                preview,
                (local_x, local_y),
                (local_x + template_width - 1, local_y + template_height - 1),
                (0, 0, 255),
                2,
            )
            cv2.putText(
                preview,
                str(index),
                (max(0, local_x - 30), local_y + template_height - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
        if not cv2.imwrite(str(resolved_preview), preview):
            raise RuntimeError(f"Cannot save detection preview: {resolved_preview}")

    for match in matches:
        match.pop("local_x", None)
        match.pop("local_y", None)
    return {
        "title": title,
        "window_rectangle": (left, top, right, bottom),
        "capture_path": str(resolved_capture) if resolved_capture else "",
        "preview_path": str(resolved_preview) if resolved_preview else "",
        "matches": matches,
    }


def scan_pattern_matches(
    window_bounds: tuple[int, int, int, int],
    pattern_path: str,
    confidence: float,
) -> list[dict]:
    """Capture one window in memory and return all template matches."""
    import cv2
    import numpy as np
    from PIL import ImageGrab

    raise_if_cancelled()
    if not 0 < confidence <= 1:
        raise ValueError("Pattern confidence must be greater than 0 and at most 1")
    resolved_pattern = configured_path(pattern_path)
    if not resolved_pattern.is_file():
        raise FileNotFoundError(f"Pattern was not found: {resolved_pattern}")

    left, top, right, bottom = map(int, window_bounds)
    screenshot = ImageGrab.grab(
        bbox=(left, top, right, bottom),
        all_screens=True,
    ).convert("RGB")
    screenshot_bgr = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_RGB2BGR)
    template = cv2.imread(str(resolved_pattern), cv2.IMREAD_UNCHANGED)
    if template is None:
        raise RuntimeError(f"Cannot read pattern: {resolved_pattern}")
    if template.ndim == 3 and template.shape[2] == 4:
        template = cv2.cvtColor(template, cv2.COLOR_BGRA2BGR)
    elif template.ndim == 2:
        template = cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)

    template_height, template_width = template.shape[:2]
    screen_height, screen_width = screenshot_bgr.shape[:2]
    if template_width > screen_width or template_height > screen_height:
        raise RuntimeError(
            f"Pattern {resolved_pattern.name} is larger than the Chrome capture"
        )

    scores = cv2.matchTemplate(
        screenshot_bgr,
        template,
        cv2.TM_CCOEFF_NORMED,
    )
    threshold_mask = (scores >= confidence).astype(np.uint8)
    component_count, labels, _stats, _centroids = (
        cv2.connectedComponentsWithStats(threshold_mask, connectivity=8)
    )
    matches: list[dict] = []
    for label in range(1, component_count):
        y_values, x_values = np.where(labels == label)
        if not len(x_values):
            continue
        best_index = int(np.argmax(scores[y_values, x_values]))
        local_x = int(x_values[best_index])
        local_y = int(y_values[best_index])
        matches.append(
            {
                "left": left + local_x,
                "top": top + local_y,
                "width": template_width,
                "height": template_height,
                "center_x": left + local_x + template_width // 2,
                "center_y": top + local_y + template_height // 2,
                "confidence": float(scores[local_y, local_x]),
                "pattern_path": str(resolved_pattern),
            }
        )
    raise_if_cancelled()
    return sorted(matches, key=lambda item: (-item["confidence"], item["top"]))


def scroll_browser_down(
    window_bounds: tuple[int, int, int, int],
    distance_pixels: int,
    settle_delay: float,
) -> None:
    """Scroll Chrome down by an approximately pixel-sized wheel distance."""
    import pyautogui

    if distance_pixels <= 0:
        raise ValueError("menu_pattern_scroll_distance must be positive")
    if settle_delay < 0:
        raise ValueError("menu_pattern_scroll_settle_delay cannot be negative")
    left, top, right, bottom = map(int, window_bounds)
    safe_x = left + max(1, (right - left) // 3)
    safe_y = top + max(1, (bottom - top) // 2)
    move_pointer_to(safe_x, safe_y)
    wheel_notches = max(1, round(distance_pixels / 100))
    pyautogui.PAUSE = 0
    pyautogui.scroll(-wheel_notches)
    cancellable_sleep(settle_delay)


def find_pattern_with_scroll(
    window_bounds: tuple[int, int, int, int],
    pattern_path: str,
    *,
    confidence: float,
    scroll_distance: int,
    scroll_attempts: int,
    scroll_settle_delay: float,
    reference_x: int | None = None,
) -> tuple[dict, int]:
    """Find a pattern, scrolling Chrome down between bounded attempts."""
    if scroll_attempts < 0:
        raise ValueError("menu_pattern_scroll_attempts cannot be negative")
    for scroll_count in range(scroll_attempts + 1):
        matches = scan_pattern_matches(
            window_bounds,
            pattern_path,
            confidence,
        )
        if matches:
            if reference_x is None:
                return matches[0], scroll_count
            closest = min(
                matches,
                key=lambda item: (
                    abs(int(item["center_x"]) - reference_x),
                    -float(item["confidence"]),
                    int(item["top"]),
                ),
            )
            return closest, scroll_count
        if scroll_count == scroll_attempts:
            break
        scroll_browser_down(
            window_bounds,
            scroll_distance,
            scroll_settle_delay,
        )
    pattern_name = configured_path(pattern_path).name
    raise RuntimeError(
        f"Pattern {pattern_name} was not found after {scroll_attempts} scroll(s)"
    )


def move_cursor_smoothly(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float,
) -> None:
    """Drag-select using PyAutoGUI's eased, non-linear speed profile."""
    import pyautogui

    if duration < 0:
        raise ValueError("selection_drag_duration cannot be negative")
    raise_if_cancelled()
    pyautogui.PAUSE = 0
    move_pointer_to(start_x, start_y)
    pyautogui.mouseDown(button="left")
    try:
        pyautogui.moveTo(
            end_x,
            end_y,
            duration=duration,
            tween=pyautogui.easeInOutQuad,
        )
    finally:
        pyautogui.mouseUp(button="left")
    raise_if_cancelled()


def move_pointer_to(end_x: int, end_y: int) -> None:
    """Move directly with smooth acceleration and deceleration."""
    import pyautogui

    raise_if_cancelled()
    pyautogui.PAUSE = 0
    pyautogui.moveTo(
        end_x,
        end_y,
        duration=random_mouse_move_duration(),
        tween=pyautogui.easeInOutQuad,
    )
    raise_if_cancelled()


def click_left_button() -> None:
    """Click with independent bounded-normal pauses before and afterwards."""
    interaction_delay("click_delay")
    raise_if_cancelled()
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    time.sleep(0.05)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
    interaction_delay("click_delay")


def send_copy_shortcut() -> None:
    """Send Ctrl+C to the foreground application."""
    raise_if_cancelled()
    USER32.keybd_event(VK_CONTROL, 0, 0, None)
    USER32.keybd_event(VK_C, 0, 0, None)
    USER32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, None)
    USER32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, None)


def press_enter() -> None:
    """Press and release Enter in the foreground application."""
    raise_if_cancelled()
    USER32.keybd_event(VK_RETURN, 0, 0, None)
    time.sleep(0.05)
    USER32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, None)


def press_f5() -> None:
    """Refresh the foreground browser page."""
    raise_if_cancelled()
    USER32.keybd_event(VK_F5, 0, 0, None)
    time.sleep(0.05)
    USER32.keybd_event(VK_F5, 0, KEYEVENTF_KEYUP, None)


def notification_rectangle(
    *,
    top_min: int,
    top_max: int,
    right_min: int,
    right_max: int,
) -> tuple[int, int, int, int]:
    """Return the configured primary-screen notification rectangle."""
    screen_width = int(USER32.GetSystemMetrics(0))
    screen_height = int(USER32.GetSystemMetrics(1))
    if not (0 <= top_min <= top_max < screen_height):
        raise ValueError(f"Invalid notification vertical range: {top_min}..{top_max}")
    if not (0 <= right_min <= right_max < screen_width):
        raise ValueError(
            f"Invalid notification right-edge range: {right_min}..{right_max}"
        )
    left = screen_width - right_max
    right = screen_width - right_min
    return left, top_min, right + 1, top_max + 1


def capture_screen_rectangle(rectangle: tuple[int, int, int, int]):
    """Capture one absolute primary-screen rectangle as an RGB array."""
    import numpy as np
    from PIL import ImageGrab

    raise_if_cancelled()
    image = ImageGrab.grab(bbox=rectangle, all_screens=True).convert("RGB")
    raise_if_cancelled()
    return np.asarray(image, dtype=np.uint8)


def wait_for_result_notification(
    baseline,
    rectangle: tuple[int, int, int, int],
    *,
    timeout: float,
    settle_delay: float,
    change_threshold: int,
    changed_ratio_threshold: float,
    red_score_threshold: float,
) -> dict:
    """Wait for the result toast and classify changed chromatic pixels."""
    import numpy as np

    if timeout <= 0 or settle_delay < 0:
        raise ValueError("Notification timing values are invalid")
    if not 0 <= change_threshold <= 255:
        raise ValueError("notification_change_threshold must be 0..255")
    if not 0 <= changed_ratio_threshold <= 1:
        raise ValueError("notification_changed_ratio must be 0..1")
    if not 0 <= red_score_threshold <= 1:
        raise ValueError("notification_red_score_threshold must be 0..1")

    deadline = time.monotonic() + timeout
    current = baseline
    changed_ratio = 0.0
    while time.monotonic() < deadline:
        current = capture_screen_rectangle(rectangle)
        difference = np.max(
            np.abs(current.astype(np.int16) - baseline.astype(np.int16)),
            axis=2,
        )
        changed_mask = difference >= change_threshold
        changed_ratio = float(np.mean(changed_mask))
        if changed_ratio >= changed_ratio_threshold:
            if settle_delay:
                cancellable_sleep(settle_delay)
                current = capture_screen_rectangle(rectangle)
                difference = np.max(
                    np.abs(
                        current.astype(np.int16) - baseline.astype(np.int16)
                    ),
                    axis=2,
                )
                changed_mask = difference >= change_threshold
                changed_ratio = float(np.mean(changed_mask))
            break
        cancellable_sleep(0.08)
    else:
        raise RuntimeError("Result notification did not appear before timeout")

    changed_pixels = current[changed_mask].astype(np.float32)
    if not len(changed_pixels):
        raise RuntimeError("Result notification contains no changed pixels")
    chroma = np.max(changed_pixels, axis=1) - np.min(changed_pixels, axis=1)
    chromatic_pixels = changed_pixels[chroma >= 20]
    if not len(chromatic_pixels):
        red_score = 0.0
    else:
        red_reference = np.array([0xEB, 0x56, 0x2A], dtype=np.float32)
        green_reference = np.array([0x28, 0xA7, 0x45], dtype=np.float32)
        red_distance = np.linalg.norm(chromatic_pixels - red_reference, axis=1)
        green_distance = np.linalg.norm(
            chromatic_pixels - green_reference,
            axis=1,
        )
        red_score = float(np.mean(red_distance < green_distance))

    colour = "red" if red_score >= red_score_threshold else "green"
    return {
        "colour": colour,
        "red_score": red_score,
        "changed_ratio": changed_ratio,
    }


def clipboard_format_name(format_id: int) -> str:
    standard_names = {
        1: "CF_TEXT",
        2: "CF_BITMAP",
        3: "CF_METAFILEPICT",
        8: "CF_DIB",
        13: "CF_UNICODETEXT",
        14: "CF_ENHMETAFILE",
        15: "CF_HDROP",
        17: "CF_DIBV5",
    }
    if format_id in standard_names:
        return standard_names[format_id]
    name_buffer = ctypes.create_unicode_buffer(256)
    length = USER32.GetClipboardFormatNameW(
        format_id,
        name_buffer,
        len(name_buffer),
    )
    return name_buffer.value if length else f"Format {format_id}"


def read_global_clipboard_bytes(format_id: int) -> bytes:
    handle = USER32.GetClipboardData(format_id)
    if not handle:
        return b""
    pointer = KERNEL32.GlobalLock(handle)
    if not pointer:
        return b""
    try:
        size = KERNEL32.GlobalSize(handle)
        return ctypes.string_at(pointer, size).rstrip(b"\x00")
    finally:
        KERNEL32.GlobalUnlock(handle)


def read_global_clipboard_text(format_id: int) -> str:
    handle = USER32.GetClipboardData(format_id)
    if not handle:
        return ""
    pointer = KERNEL32.GlobalLock(handle)
    if not pointer:
        return ""
    try:
        return ctypes.wstring_at(pointer)
    finally:
        KERNEL32.GlobalUnlock(handle)


def read_clipboard() -> tuple[str, str, list[str]]:
    """Read plain text, CF_HTML and all advertised clipboard format names."""
    deadline = time.monotonic() + 2
    while not USER32.OpenClipboard(None):
        if time.monotonic() >= deadline:
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.05)

    try:
        format_ids = []
        current_format = 0
        while True:
            current_format = USER32.EnumClipboardFormats(current_format)
            if not current_format:
                break
            format_ids.append(int(current_format))

        plain_text = (
            read_global_clipboard_text(CF_UNICODETEXT)
            if CF_UNICODETEXT in format_ids
            else ""
        )
        html_format = int(USER32.RegisterClipboardFormatW("HTML Format"))
        html_bytes = (
            read_global_clipboard_bytes(html_format)
            if html_format in format_ids
            else b""
        )
        html = html_bytes.decode("utf-8", errors="replace")
        names = [clipboard_format_name(format_id) for format_id in format_ids]
        return plain_text, html, names
    finally:
        USER32.CloseClipboard()


PRICE_PATTERN = re.compile(
    r"(?<![\d.,])\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{2})\s*€",
    re.IGNORECASE,
)


def classify_css_colour(value: str) -> str:
    """Reduce a CSS colour token to green, red or its original value."""
    normalized = value.casefold().strip()
    if normalized in {"green", "lime", "#008000", "#00ff00"}:
        return "green"
    if normalized in {"red", "#ff0000", "#f00"}:
        return "red"

    rgb_match = re.fullmatch(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
        normalized,
    )
    if rgb_match:
        red, green, blue = map(int, rgb_match.groups())
        if green > red * 1.2 and green > blue * 1.2:
            return "green"
        if red > green * 1.2 and red > blue * 1.2:
            return "red"
    return value


def detect_price_and_colour(plain_text: str, html: str) -> tuple[str, str, str]:
    """Extract the copied euro price and its nearest HTML colour marker."""
    text_price = PRICE_PATTERN.search(plain_text)
    html_prices = list(PRICE_PATTERN.finditer(html))
    price = " ".join(text_price.group(0).split()) if text_price else ""
    if not price and html_prices:
        price = " ".join(html_prices[-1].group(0).split())
    if not html_prices:
        return price, "unknown", "HTML price markup was not available"

    price_position = html_prices[-1].start()
    context_start = max(0, price_position - 1200)
    context = html[context_start:price_position]
    lowered = context.casefold()
    success_position = lowered.rfind("text-success")
    danger_position = lowered.rfind("text-danger")
    if success_position >= 0 or danger_position >= 0:
        if success_position > danger_position:
            return price, "green", "nearest HTML class: text-success"
        return price, "red", "nearest HTML class: text-danger"

    css_colours = list(
        re.finditer(
            r"color\s*:\s*(#[0-9a-f]{3,8}|rgba?\([^)]*\)|[a-z]+)",
            context,
            re.IGNORECASE,
        )
    )
    if css_colours:
        raw_colour = css_colours[-1].group(1)
        return price, classify_css_colour(raw_colour), f"CSS colour: {raw_colour}"
    return price, "unknown", "no colour marker found in clipboard HTML"


def select_and_copy_order(
    match: dict,
    *,
    start_offset: int,
    vertical_offset: int,
    drag_distance: int,
    drag_duration: float,
) -> dict:
    """Select one order row vertically, copy it and inspect clipboard formats."""
    if start_offset < 0 or drag_distance <= 0:
        raise ValueError("Selection offsets must be positive")

    start_x = int(match["left"]) - start_offset
    start_y = int(match["center_y"]) + vertical_offset
    end_x = start_x
    end_y = start_y - drag_distance
    if start_x < 0 or start_y < 0 or end_y < 0:
        raise RuntimeError(
            "Selection coordinates are outside the screen: "
            f"({start_x}, {start_y}) -> ({end_x}, {end_y})"
        )

    move_cursor_smoothly(
        start_x,
        start_y,
        end_x,
        end_y,
        drag_duration,
    )
    cancellable_sleep(0.15)
    previous_sequence = USER32.GetClipboardSequenceNumber()
    send_copy_shortcut()
    deadline = time.monotonic() + 2
    while (
        USER32.GetClipboardSequenceNumber() == previous_sequence
        and time.monotonic() < deadline
    ):
        cancellable_sleep(0.05)
    if USER32.GetClipboardSequenceNumber() == previous_sequence:
        raise RuntimeError("Ctrl+C did not update the clipboard")

    plain_text, html, format_names = read_clipboard()
    if not plain_text and not html:
        raise RuntimeError("The copied selection did not contain text or HTML")

    price, price_colour, colour_source = detect_price_and_colour(
        plain_text,
        html,
    )
    return {
        "start_x": start_x,
        "start_y": start_y,
        "end_x": end_x,
        "end_y": end_y,
        "text": plain_text,
        "html": html,
        "formats": format_names,
        "price": price,
        "price_colour": price_colour,
        "colour_source": colour_source,
    }


def open_dropdown_and_mark_checkbox(
    match: dict,
    window_bounds: tuple[int, int, int, int],
    *,
    checkbox_pattern_path: str,
    pattern_confidence: float,
    pattern_edge_inset: int,
    dropdown_edge_inset: int,
    scroll_distance: int,
    scroll_attempts: int,
    scroll_settle_delay: float,
) -> dict:
    """Open one dropdown, locate its checkbox and point inside the match."""
    dropdown_x, dropdown_y = random_dropdown_point(match, dropdown_edge_inset)
    left, top, right, bottom = map(int, window_bounds)
    if not (left <= dropdown_x < right and top <= dropdown_y < bottom):
        raise RuntimeError(
            f"Dropdown target is outside Chrome: ({dropdown_x}, {dropdown_y})"
        )

    move_pointer_to(dropdown_x, dropdown_y)
    click_left_button()
    checkbox_match, scroll_count = find_pattern_with_scroll(
        window_bounds,
        checkbox_pattern_path,
        confidence=pattern_confidence,
        scroll_distance=scroll_distance,
        scroll_attempts=scroll_attempts,
        scroll_settle_delay=scroll_settle_delay,
        reference_x=int(match["center_x"]),
    )
    checkbox_x, checkbox_y = random_pattern_point(
        checkbox_match,
        pattern_edge_inset,
        "Checkbox",
    )
    move_pointer_to(checkbox_x, checkbox_y)
    return {
        "dropdown_x": dropdown_x,
        "dropdown_y": dropdown_y,
        "checkbox_x": checkbox_x,
        "checkbox_y": checkbox_y,
        "checkbox_match": checkbox_match,
        "checkbox_scroll_count": scroll_count,
    }


def confirm_checkbox_and_mark_action(
    match: dict,
    dropdown_state: dict,
    window_bounds: tuple[int, int, int, int],
    *,
    price_colour: str,
    checkbox_vertical_offset: int,
    dropdown_pattern_path: str,
    red_action_pattern_path: str,
    green_action_pattern_path: str,
    pattern_confidence: float,
    pattern_edge_inset: int,
    dropdown_edge_inset: int,
    scroll_distance: int,
    scroll_attempts: int,
    scroll_settle_delay: float,
    notification_top_min: int,
    notification_top_max: int,
    notification_right_min: int,
    notification_right_max: int,
    notification_timeout: float,
    notification_settle_delay: float,
    notification_change_threshold: int,
    notification_changed_ratio: float,
    notification_red_score_threshold: float,
    red_refresh_wait: float,
) -> dict:
    """Click pattern-detected checkbox and colour-dependent action."""

    normalized_colour = price_colour.casefold().strip()
    if normalized_colour not in {"red", "green"}:
        raise RuntimeError(
            f"Cannot choose an action for unknown price colour: {price_colour}"
        )
    if checkbox_vertical_offset <= 0:
        raise ValueError("checkbox_vertical_offset must be positive")
    if red_refresh_wait < 0:
        raise ValueError("red_refresh_wait cannot be negative")
    checkbox_x = int(dropdown_state["checkbox_x"])
    checkbox_y = int(dropdown_state["checkbox_y"])
    checkbox_match = dropdown_state["checkbox_match"]
    left, top, right, bottom = map(int, window_bounds)
    if not (left <= checkbox_x < right and top <= checkbox_y < bottom):
        raise RuntimeError(
            f"Checkbox target is outside Chrome: ({checkbox_x}, {checkbox_y})"
        )

    move_pointer_to(checkbox_x, checkbox_y)
    click_left_button()
    interaction_delay("enter_delay")
    press_enter()

    dropdown_matches = scan_pattern_matches(
        window_bounds,
        dropdown_pattern_path,
        pattern_confidence,
    )
    if not dropdown_matches:
        raise RuntimeError("Dropdown pattern was not found after Checkbox confirmation")
    expected_dropdown_x = int(match["center_x"])
    expected_dropdown_y = int(checkbox_match["center_y"]) - checkbox_vertical_offset
    current_dropdown = min(
        dropdown_matches,
        key=lambda item: (
            (int(item["center_x"]) - expected_dropdown_x) ** 2
            + (int(item["center_y"]) - expected_dropdown_y) ** 2
        ),
    )
    dropdown_x, dropdown_y = random_dropdown_point(
        current_dropdown,
        dropdown_edge_inset,
    )
    move_pointer_to(dropdown_x, dropdown_y)
    click_left_button()
    action_pattern_path = (
        green_action_pattern_path
        if normalized_colour == "green"
        else red_action_pattern_path
    )
    action_match, action_scroll_count = find_pattern_with_scroll(
        window_bounds,
        action_pattern_path,
        confidence=pattern_confidence,
        scroll_distance=scroll_distance,
        scroll_attempts=scroll_attempts,
        scroll_settle_delay=scroll_settle_delay,
        reference_x=int(current_dropdown["center_x"]),
    )
    action_x, action_y = random_pattern_point(
        action_match,
        pattern_edge_inset,
        "Action button",
    )
    move_pointer_to(action_x, action_y)

    notification_bounds = notification_rectangle(
        top_min=notification_top_min,
        top_max=notification_top_max,
        right_min=notification_right_min,
        right_max=notification_right_max,
    )
    notification_baseline = capture_screen_rectangle(notification_bounds)

    click_left_button()
    interaction_delay("enter_delay")
    press_enter()
    notification = wait_for_result_notification(
        notification_baseline,
        notification_bounds,
        timeout=notification_timeout,
        settle_delay=notification_settle_delay,
        change_threshold=notification_change_threshold,
        changed_ratio_threshold=notification_changed_ratio,
        red_score_threshold=notification_red_score_threshold,
    )
    notification_x = truncated_normal_pixel(
        notification_bounds[0],
        notification_bounds[2] - 1,
    )
    notification_y = truncated_normal_pixel(
        notification_bounds[1],
        notification_bounds[3] - 1,
    )
    move_pointer_to(notification_x, notification_y)
    click_left_button()

    refreshed = notification["colour"] == "red"
    if refreshed:
        press_f5()
        cancellable_sleep(red_refresh_wait)
    return {
        "price_colour": normalized_colour,
        "checkbox_x": checkbox_x,
        "checkbox_y": checkbox_y,
        "dropdown_x": dropdown_x,
        "dropdown_y": dropdown_y,
        "action_x": action_x,
        "action_y": action_y,
        "action_clicked": True,
        "action_confirmed": True,
        "notification_colour": notification["colour"],
        "notification_red_score": notification["red_score"],
        "notification_changed_ratio": notification["changed_ratio"],
        "notification_x": notification_x,
        "notification_y": notification_y,
        "page_refreshed": refreshed,
        "checkbox_scroll_count": int(
            dropdown_state.get("checkbox_scroll_count", 0)
        ),
        "action_scroll_count": action_scroll_count,
        "action_pattern": configured_path(action_pattern_path).name,
    }


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
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Playwright is required for the finish part N browser mode"
        ) from error

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
