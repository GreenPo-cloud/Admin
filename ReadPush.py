"""Windows notification reader used by Admin.py.

Slack does not expose application metadata on the target computer, so the
listener prints the sender/title and message from every new Windows toast. It
does not connect to Slack, modify notifications or dismiss them.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Callable


POLL_INTERVAL_SECONDS = 0.5


def _console_marker() -> str:
    marker = chr(0x1F535)
    try:
        marker.encode(sys.stdout.encoding or "utf-8")
        return marker
    except UnicodeEncodeError:
        return "\033[96m●\033[0m" if sys.stdout.isatty() else "[PUSH]"


BLUE_MARKER = _console_marker()
OTHER_MARKER = "[PUSH]"

WINRT_PACKAGES = (
    "winrt-Windows.Foundation",
    "winrt-Windows.Foundation.Collections",
    "winrt-Windows.UI.Notifications",
    "winrt-Windows.UI.Notifications.Management",
)


def _load_winrt_types():
    try:
        management = importlib.import_module("winrt.windows.ui.notifications.management")
        notifications = importlib.import_module("winrt.windows.ui.notifications")
    except ImportError:
        print(f"{BLUE_MARKER} Installing Windows notification support...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *WINRT_PACKAGES]
            )
        except Exception as error:
            package_list = " ".join(WINRT_PACKAGES)
            raise RuntimeError(
                "cannot install PyWinRT packages. Run: "
                f'"{sys.executable}" -m pip install {package_list}'
            ) from error
        importlib.invalidate_caches()
        management = importlib.import_module("winrt.windows.ui.notifications.management")
        notifications = importlib.import_module("winrt.windows.ui.notifications")

    return (
        management.UserNotificationListener,
        management.UserNotificationListenerAccessStatus,
        notifications.NotificationKinds,
        notifications.KnownNotificationBindings,
    )


def _enum_member(enum_type: Any, name: str) -> Any:
    """Support both current uppercase and older lowercase PyWinRT enums."""
    for candidate in (name, name.lower()):
        if hasattr(enum_type, candidate):
            return getattr(enum_type, candidate)
    raise AttributeError(f"{enum_type.__name__}.{name} is unavailable")


def _safe_attr(value: Any, attribute: str, default: Any = None) -> Any:
    try:
        result = getattr(value, attribute)
        return default if result is None else result
    except Exception:
        return default


def _safe_text(value: Any, default: str = "not available") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
        return text or default
    except Exception:
        return default


def _format_time(value: Any) -> str:
    if value is None:
        return "not available"
    try:
        if isinstance(value, datetime):
            return value.astimezone().strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]
        return str(value)
    except Exception:
        return "not available"


def _app_metadata(notification: Any) -> dict[str, str]:
    app_info = _safe_attr(notification, "app_info")
    display_info = _safe_attr(app_info, "display_info")
    package = _safe_attr(app_info, "package")
    package_id = _safe_attr(package, "id")
    return {
        "display_name": _safe_text(_safe_attr(display_info, "display_name")),
        "description": _safe_text(_safe_attr(display_info, "description")),
        "app_user_model_id": _safe_text(_safe_attr(app_info, "app_user_model_id")),
        "app_id": _safe_text(_safe_attr(app_info, "id")),
        "package_family_name": _safe_text(
            _safe_attr(app_info, "package_family_name")
        ),
        "package_name": _safe_text(_safe_attr(package_id, "name")),
        "package_full_name": _safe_text(_safe_attr(package_id, "full_name")),
    }


def _binding_details(notification: Any, toast_generic: Any) -> list[dict[str, Any]]:
    content = _safe_attr(notification, "notification")
    visual = _safe_attr(content, "visual")
    bindings: list[Any] = []
    try:
        bindings = list(visual.bindings)
    except Exception:
        pass

    # Some providers expose ToastGeneric through GetBinding but do not enumerate
    # it reliably through Bindings. Include that fallback without duplicating it.
    if not bindings and visual is not None:
        try:
            generic = visual.get_binding(toast_generic)
            if generic is not None:
                bindings.append(generic)
        except Exception:
            pass

    result: list[dict[str, Any]] = []
    for binding in bindings:
        texts: list[str] = []
        try:
            texts = [_safe_text(item.text, "") for item in binding.get_text_elements()]
            texts = [text for text in texts if text]
        except Exception as error:
            texts = [f"<text extraction error: {error}>"]
        result.append(
            {
                "template": _safe_text(_safe_attr(binding, "template")),
                "language": _safe_text(_safe_attr(binding, "language")),
                "texts": texts,
            }
        )
    return result


def _notification_record(notification: Any, toast_generic: Any) -> dict[str, Any]:
    app = _app_metadata(notification)
    content = _safe_attr(notification, "notification")
    visual = _safe_attr(content, "visual")
    return {
        "id": int(_safe_attr(notification, "id", -1)),
        "creation_time": _format_time(_safe_attr(notification, "creation_time")),
        "expiration_time": _format_time(_safe_attr(content, "expiration_time")),
        "visual_language": _safe_text(_safe_attr(visual, "language")),
        "app": app,
        "bindings": _binding_details(notification, toast_generic),
    }


def _all_texts(record: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        text
        for binding in record["bindings"]
        for text in binding["texts"]
    )


def _identity(record: dict[str, Any]) -> tuple[str, int]:
    app = record["app"]
    app_identity = next(
        (
            value
            for value in (
                app["app_user_model_id"],
                app["package_family_name"],
                app["app_id"],
                app["display_name"],
            )
            if value != "not available"
        ),
        "unknown application",
    )
    return app_identity, record["id"]


def _fingerprint(record: dict[str, Any]) -> tuple[Any, ...]:
    """Include content so an updated Slack toast is treated as a new event."""
    binding_values = tuple(
        (binding["template"], binding["language"], tuple(binding["texts"]))
        for binding in record["bindings"]
    )
    return (
        record["creation_time"],
        record["expiration_time"],
        record["visual_language"],
        binding_values,
    )


def _notification_candidates(
    record: dict[str, Any],
) -> tuple[tuple[str | None, str, str], ...]:
    """Return possible channel/sender/message interpretations of a toast.

    Slack uses two different two-text layouts on the target computers:
    direct messages are ``sender`` + ``message``, while channel messages are
    ``channel`` + ``sender: message``.  Both candidates are returned so Admin's
    sender allow-list can select the correct interpretation without breaking a
    direct message whose body happens to contain a colon.
    """
    texts = _all_texts(record)
    if not texts:
        return ()

    if len(texts) >= 3:
        channel = texts[0]
        sender = texts[1]
        message = " | ".join(texts[2:])
        return ((channel, sender, message),)

    if len(texts) == 2:
        title, body = texts
        candidates: list[tuple[str | None, str, str]] = []
        possible_sender, separator, possible_message = body.partition(":")
        if separator and possible_sender.strip():
            candidates.append(
                (title, possible_sender.strip(), possible_message.lstrip())
            )
        candidates.append((None, title, body))
        return tuple(candidates)

    return ((None, texts[0], ""),)


def _print_notification(channel: str | None, sender: str, message: str) -> None:
    print()
    channel_prefix = f"[{channel}] " if channel else ""
    print(
        f"{BLUE_MARKER} {channel_prefix}{sender}: {message}"
        if message
        else f"{BLUE_MARKER} {channel_prefix}{sender}"
    )
    print("> ", end="", flush=True)


def _print_text_debug(record: dict[str, Any]) -> None:
    texts = _all_texts(record)
    print()
    print(f"{OTHER_MARKER} Text elements ({len(texts)}):")
    for index, text in enumerate(texts, start=1):
        print(f"  Text {index}: {text}")
    print("> ", end="", flush=True)


def _snapshot(
    notifications: Any, toast_generic: Any
) -> dict[tuple[str, int], dict[str, Any]]:
    snapshot: dict[tuple[str, int], dict[str, Any]] = {}
    for notification in notifications:
        try:
            record = _notification_record(notification, toast_generic)
            snapshot[_identity(record)] = record
        except Exception as error:
            print(f"{OTHER_MARKER} Cannot decode one Windows notification: {error}")
    return snapshot


async def _request_access(listener: Any, allowed_status: Any) -> bool:
    try:
        status = listener.get_access_status()
        if status == allowed_status:
            return True
        status = await listener.request_access_async()
        if status == allowed_status:
            return True
        print(f"{BLUE_MARKER} Notification access was not allowed")
        return False
    except Exception as error:
        print(
            f"{BLUE_MARKER} Cannot request Windows notification access: {error}. "
            "This Windows configuration may block an unpackaged Python program."
        )
        return False


async def _listen(
    stop_event: threading.Event,
    notification_filter: Callable[[str], bool] | None,
    on_notification: Callable[[str, str], None] | None,
    debug_texts: bool,
) -> None:
    (
        UserNotificationListener,
        UserNotificationListenerAccessStatus,
        NotificationKinds,
        KnownNotificationBindings,
    ) = _load_winrt_types()

    listener = UserNotificationListener.current
    allowed = _enum_member(UserNotificationListenerAccessStatus, "ALLOWED")
    toast = _enum_member(NotificationKinds, "TOAST")
    toast_generic = _enum_member(KnownNotificationBindings, "TOAST_GENERIC")
    if not await _request_access(listener, allowed):
        return

    initial_items = await listener.get_notifications_async(toast)
    known = _snapshot(initial_items, toast_generic)
    print(f"{BLUE_MARKER} Windows notification listener started")

    consecutive_errors = 0
    while not stop_event.is_set():
        try:
            current_items = await listener.get_notifications_async(toast)
            current = _snapshot(current_items, toast_generic)

            for identity, record in current.items():
                previous = known.get(identity)
                if previous is None or _fingerprint(previous) != _fingerprint(record):
                    if debug_texts:
                        _print_text_debug(record)
                    candidates = _notification_candidates(record)
                    if not candidates:
                        continue

                    fields = candidates[0] if notification_filter is None else None
                    if notification_filter is not None:
                        for candidate in candidates:
                            try:
                                if notification_filter(candidate[1]):
                                    fields = candidate
                                    break
                            except Exception as error:
                                print(
                                    f"{OTHER_MARKER} Notification filter failed: "
                                    f"{error}"
                                )
                                fields = None
                                break
                    if fields is None:
                        continue

                    channel, sender, message = fields
                    if not sender:
                        continue
                    _print_notification(channel, sender, message)
                    if on_notification is not None:
                        try:
                            on_notification(sender, message)
                        except Exception as error:
                            print(f"{OTHER_MARKER} Notification callback failed: {error}")

            # Keeping only the current snapshot lets an identical notification be
            # detected again after it is dismissed and later recreated.
            known = current
            consecutive_errors = 0
        except Exception as error:
            consecutive_errors += 1
            print(
                f"{BLUE_MARKER} Windows push read error: {error} "
                f"(attempt {consecutive_errors}/3)"
            )
            if consecutive_errors >= 3:
                print(
                    f"{BLUE_MARKER} Push listener disabled after repeated Windows "
                    "API errors; the rest of Admin remains active"
                )
                return
            await asyncio.sleep(5)
            continue
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _thread_main(
    stop_event: threading.Event,
    notification_filter: Callable[[str], bool] | None,
    on_notification: Callable[[str, str], None] | None,
    debug_texts: bool,
) -> None:
    try:
        asyncio.run(
            _listen(stop_event, notification_filter, on_notification, debug_texts)
        )
    except Exception as error:
        print(f"{BLUE_MARKER} Windows push listener stopped: {error}")


def start_slack_notification_listener(
    stop_event: threading.Event,
    notification_filter: Callable[[str], bool] | None = None,
    on_notification: Callable[[str, str], None] | None = None,
    debug_texts: bool = False,
) -> threading.Thread | None:
    """Request access on the main thread, then start the diagnostic listener."""
    (
        UserNotificationListener,
        UserNotificationListenerAccessStatus,
        _NotificationKinds,
        _KnownNotificationBindings,
    ) = _load_winrt_types()
    listener = UserNotificationListener.current
    allowed = _enum_member(UserNotificationListenerAccessStatus, "ALLOWED")
    if not asyncio.run(_request_access(listener, allowed)):
        return None

    thread = threading.Thread(
        target=_thread_main,
        args=(stop_event, notification_filter, on_notification, debug_texts),
        name="Windows notification listener",
        daemon=True,
    )
    thread.start()
    return thread


if __name__ == "__main__":
    standalone_stop = threading.Event()
    print(f"{BLUE_MARKER} Press Ctrl+C to stop")
    worker = start_slack_notification_listener(standalone_stop)
    if worker is None:
        raise SystemExit(1)
    try:
        while worker.is_alive():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        standalone_stop.set()
        worker.join(timeout=3)
