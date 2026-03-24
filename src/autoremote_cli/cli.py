#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""
AutoRemote client and CLI.

This single-file module can be used both as:
- a Python library
- a command-line tool

Features:
- Notifications: /sendnotification
- Messages: /sendmessage
- Intents: /sendintent
- TOML configuration
- Named devices
- Optional argcomplete support
- URL preview mode
- Rich message composition helpers

Configuration
-------------

By default the CLI reads:

    $XDG_CONFIG_HOME/autoremote/config.toml

or, if XDG_CONFIG_HOME is not set:

    ~/.config/autoremote/config.toml

Example configuration:

    default_device = "phone"

    [defaults]
    timeout = 5
    retries = 1
    retry_delay = 1.0
    ttl = 86400

    [devices.phone]
    key = "YOUR_DEVICE_KEY"
    password = "optional-password"

    [devices.tablet]
    key = "ANOTHER_KEY"
    sender = "phone"

Argcomplete activation
----------------------

Install argcomplete and enable completion, for example:

    pip install argcomplete
    activate-global-python-argcomplete --user

or for one script:

    eval "$(register-python-argcomplete autoremote.py)"
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Iterable, Sequence

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Python 3.11+ is required (tomllib is missing).") from exc

try:  # pragma: no cover - optional dependency
    import argcomplete
except ImportError:  # pragma: no cover
    argcomplete = None


DEFAULT_BASE_URL = "https://autoremotejoaomgcd.appspot.com"
DEFAULT_SEPARATOR = "=:="


class AutoRemoteError(Exception):
    """Base AutoRemote error."""


class AutoRemoteConfigError(AutoRemoteError):
    """Configuration error."""


class AutoRemoteUsageError(AutoRemoteError):
    """User input or composition error."""


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config"


DEFAULT_CONFIG_PATH = _xdg_config_home() / "autoremote" / "config.toml"


def _load_toml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise AutoRemoteConfigError(f"Could not read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AutoRemoteConfigError(f"Top-level TOML object must be a table: {path}")
    return data


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        inner = ", ".join(_toml_literal(item) for item in value)
        return f"[{inner}]"
    raise AutoRemoteConfigError(f"Unsupported value type in config: {type(value).__name__}")


def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []

    def emit_table(table: dict[str, Any], prefix: list[str] | None = None) -> None:
        current_prefix = prefix or []
        scalar_items: list[tuple[str, Any]] = []
        table_items: list[tuple[str, dict[str, Any]]] = []

        for key, value in table.items():
            if isinstance(value, dict):
                table_items.append((key, value))
            else:
                scalar_items.append((key, value))

        if current_prefix:
            lines.append(f"[{'.'.join(current_prefix)}]")

        for key, value in scalar_items:
            lines.append(f"{key} = {_toml_literal(value)}")

        if scalar_items and table_items:
            lines.append("")

        for index, (key, child) in enumerate(table_items):
            emit_table(child, current_prefix + [key])
            if index != len(table_items) - 1:
                lines.append("")

    emit_table(data)
    return "\n".join(lines).strip() + "\n"


def _save_toml_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = _dump_toml(data)
    try:
        path.write_text(serialized, encoding="utf-8")
    except OSError as exc:
        raise AutoRemoteConfigError(f"Could not write config file {path}: {exc}") from exc


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value)


def _flatten_string_items(value: Any) -> list[str]:
    """
    Flatten strings / iterables of strings / nested iterables into a flat list of strings.
    None values are ignored.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (bytes, bytearray)):
        return [value.decode()]
    if isinstance(value, Sequence):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_string_items(item))
        return result
    return [str(value)]


def _parse_json_string_array(text: str, *, option_name: str) -> list[str]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutoRemoteUsageError(
            f"{option_name} must contain a valid JSON array of strings."
        ) from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AutoRemoteUsageError(
            f"{option_name} must contain a JSON array of strings."
        )
    return value


def _clean_params(params: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                cleaned[key] = "1"
            continue
        cleaned[key] = str(value)
    return cleaned


def _merge_dicts(*dicts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for current in dicts:
        merged.update(current)
    return merged


def _coerce_number(value: Any, cast: type, *, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, cast):
        return value
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise AutoRemoteConfigError(f"{name!r} must be a {cast.__name__}.") from exc


class AutoRemote:
    """
    AutoRemote client.

    The client can be created directly:

        client = AutoRemote(key="...")

    or from a TOML configuration file:

        client = AutoRemote.from_config(device="phone")
    """

    NOTIFY_ALLOWED_DEFAULTS = {
        "password",
        "sender",
        "ttl",
        "collapseKey",
    }

    MESSAGE_ALLOWED_DEFAULTS = {
        "password",
        "sender",
        "target",
        "ttl",
        "collapseKey",
    }

    INTENT_ALLOWED_DEFAULTS = {
        "password",
        "sender",
        "ttl",
        "collapseKey",
        "target",
    }

    def __init__(
        self,
        key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 5,
        retries: int = 0,
        retry_delay: float = 1.0,
        default_params: dict[str, Any] | None = None,
        config_path: Path | None = None,
        device_name: str | None = None,
    ) -> None:
        if not key:
            raise AutoRemoteConfigError("AutoRemote key is required.")
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.separator = DEFAULT_SEPARATOR
        self.default_params = dict(default_params or {})
        self.config_path = config_path
        self.device_name = device_name

    @classmethod
    def from_config(
        cls,
        *,
        device: str | None = None,
        path: str | os.PathLike[str] | None = None,
        key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        retry_delay: float | None = None,
    ) -> "AutoRemote":
        config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
        config = _load_toml_file(config_path)

        defaults = config.get("defaults", {})
        if defaults and not isinstance(defaults, dict):
            raise AutoRemoteConfigError("'defaults' must be a table.")

        devices = config.get("devices", {})
        if devices and not isinstance(devices, dict):
            raise AutoRemoteConfigError("'devices' must be a table.")

        selected_device = device or config.get("default_device")
        device_cfg: dict[str, Any] = {}
        if selected_device:
            device_cfg = devices.get(selected_device, {})
            if not device_cfg:
                raise AutoRemoteConfigError(
                    f"Device {selected_device!r} was not found in {config_path}."
                )
            if not isinstance(device_cfg, dict):
                raise AutoRemoteConfigError(
                    f"Device {selected_device!r} must be a TOML table."
                )

        root_defaults = {
            "timeout": config.get("timeout"),
            "retries": config.get("retries"),
            "retry_delay": config.get("retry_delay"),
            "key": config.get("key"),
        }

        merged_defaults = _merge_dicts(defaults, device_cfg)

        resolved_key = key or merged_defaults.get("key") or root_defaults.get("key")
        resolved_base_url = (
            base_url
            or merged_defaults.get("base_url")
            or DEFAULT_BASE_URL
        )
        resolved_timeout = timeout
        if resolved_timeout is None:
            resolved_timeout = (
                merged_defaults.get("timeout")
                if merged_defaults.get("timeout") is not None
                else root_defaults.get("timeout", 5)
            )
        resolved_retries = retries
        if resolved_retries is None:
            resolved_retries = (
                merged_defaults.get("retries")
                if merged_defaults.get("retries") is not None
                else root_defaults.get("retries", 0)
            )
        resolved_retry_delay = retry_delay
        if resolved_retry_delay is None:
            resolved_retry_delay = (
                merged_defaults.get("retry_delay")
                if merged_defaults.get("retry_delay") is not None
                else root_defaults.get("retry_delay", 1.0)
            )
        resolved_timeout = _coerce_number(resolved_timeout, float, name="timeout")
        resolved_retries = _coerce_number(resolved_retries, int, name="retries")
        resolved_retry_delay = _coerce_number(
            resolved_retry_delay, float, name="retry_delay"
        )

        endpoint_defaults = {
            key_: value
            for key_, value in merged_defaults.items()
            if key_
            not in {
                "key",
                "base_url",
                "timeout",
                "retries",
                "retry_delay",
            }
        }

        return cls(
            key=resolved_key,
            base_url=resolved_base_url,
            timeout=resolved_timeout,
            retries=resolved_retries,
            retry_delay=resolved_retry_delay,
            default_params=endpoint_defaults,
            config_path=config_path,
            device_name=selected_device,
        )

    @staticmethod
    def build_composed_message(
        *,
        raw: str | Sequence[str] | None = None,
        left: Any = None,
        right: Any = None,
        separator: str = DEFAULT_SEPARATOR,
        left_join: str = " ",
        right_join: str = " ",
    ) -> str:
        """
        Build a message string.

        Examples:
            raw="hello"
                -> "hello"

            left=["param1", "param2"], right="command"
                -> "param1 param2=:=command"

            left="command", right=["data1", "data2"]
                -> "command=:=data1 data2"
        """
        if raw is not None and (left is not None or right is not None):
            raise AutoRemoteUsageError(
                "Use either raw=... or left/right composition, not both."
            )

        if raw is not None:
            raw_tokens = _flatten_string_items(raw)
            if not raw_tokens:
                raise AutoRemoteUsageError("Raw message cannot be empty.")
            return " ".join(raw_tokens)

        left_tokens = _flatten_string_items(left)
        right_tokens = _flatten_string_items(right)

        if left_tokens and right_tokens:
            return f"{left_join.join(left_tokens)}{separator}{right_join.join(right_tokens)}"
        if right_tokens:
            return right_join.join(right_tokens)
        if left_tokens:
            return left_join.join(left_tokens)

        raise AutoRemoteUsageError("No message content was provided.")

    def build_url(self, endpoint: str, **params: Any) -> str:
        cleaned = _clean_params(params)
        cleaned["key"] = self.key
        query = urllib.parse.urlencode(cleaned)
        return f"{self.base_url}/{endpoint.lstrip('/')}?{query}"

    def _request(self, endpoint: str, **params: Any) -> str:
        url = self.build_url(endpoint, **params)
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as response:
                    payload = response.read()
                text = payload.decode("utf-8", errors="replace").strip()
                if text != "OK":
                    raise AutoRemoteError(
                        f"AutoRemote API error on {endpoint}: {text or '<empty response>'}"
                    )
                return text
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.retry_delay)

        raise AutoRemoteError(f"Request failed: {last_error}") from last_error

    def _merge_endpoint_defaults(
        self, allowed: set[str], explicit: dict[str, Any]
    ) -> dict[str, Any]:
        defaults = {
            key: value
            for key, value in self.default_params.items()
            if key in allowed and value is not None
        }
        return _merge_dicts(defaults, explicit)

    def notify(self, **params: Any) -> str:
        merged = self._merge_endpoint_defaults(self.NOTIFY_ALLOWED_DEFAULTS, params)

        cancel_value = merged.get("cancel")
        if cancel_value:
            if not merged.get("id"):
                raise AutoRemoteUsageError(
                    "Cancelling a notification requires an id."
                )
            invalid = {
                key: value
                for key, value in merged.items()
                if key not in {"id", "cancel", "password", "sender", "ttl", "collapseKey"}
                and value not in (None, "", False)
            }
            if invalid:
                names = ", ".join(sorted(invalid))
                raise AutoRemoteUsageError(
                    f"cancel is incompatible with other notification fields: {names}"
                )
        elif not any(str(merged.get(field, "")).strip() for field in ("title", "text", "message")):
            raise AutoRemoteUsageError(
                "Notification cannot be empty. Use at least --title, --text, or --message."
            )

        return self._request("sendnotification", **merged)

    def cancel_notification(self, notification_id: str) -> str:
        return self.notify(id=notification_id, cancel=True)

    def message(
        self,
        message: str | Sequence[str] | None = None,
        *,
        raw: str | Sequence[str] | None = None,
        left: Any = None,
        right: Any = None,
        params: Any = None,
        command: Any = None,
        data: Any = None,
        left_join: str = " ",
        right_join: str = " ",
        **request_params: Any,
    ) -> str:
        """
        Send a message.

        Supported styles:

            message("raw text")

            message(left=["param1", "param2"], right="command")

            message(params=["param1", "param2"], command="command")

            message(left="command", data=["value1", "value2"])

        Notes:
        - `params` is an alias for `left`.
        - `command` and `data` are appended to the right-hand side.
        - `message=` and `raw=` are equivalent raw-message inputs.
        """
        if message is not None and raw is not None:
            raise AutoRemoteUsageError("Use either message=... or raw=..., not both.")

        if raw is None:
            raw = message

        left_tokens = _flatten_string_items(left) + _flatten_string_items(params)
        right_tokens = (
            _flatten_string_items(right)
            + _flatten_string_items(command)
            + _flatten_string_items(data)
        )
        built_message = self.build_composed_message(
            raw=raw,
            left=left_tokens if left_tokens else None,
            right=right_tokens if right_tokens else None,
            separator=self.separator,
            left_join=left_join,
            right_join=right_join,
        )

        merged = self._merge_endpoint_defaults(
            self.MESSAGE_ALLOWED_DEFAULTS,
            _merge_dicts({"message": built_message}, request_params),
        )
        return self._request("sendmessage", **merged)

    def intent(self, intent: str, **params: Any) -> str:
        if not intent:
            raise AutoRemoteUsageError("Intent cannot be empty.")
        merged = self._merge_endpoint_defaults(
            self.INTENT_ALLOWED_DEFAULTS,
            _merge_dicts({"intent": intent}, params),
        )
        return self._request("sendintent", **merged)

    def notify_url(self, **params: Any) -> str:
        merged = self._merge_endpoint_defaults(self.NOTIFY_ALLOWED_DEFAULTS, params)
        return self.build_url("sendnotification", **merged)

    def message_url(
        self,
        message: str | Sequence[str] | None = None,
        *,
        raw: str | Sequence[str] | None = None,
        left: Any = None,
        right: Any = None,
        params: Any = None,
        command: Any = None,
        data: Any = None,
        left_join: str = " ",
        right_join: str = " ",
        **request_params: Any,
    ) -> str:
        if message is not None and raw is not None:
            raise AutoRemoteUsageError("Use either message=... or raw=..., not both.")
        if raw is None:
            raw = message

        left_tokens = _flatten_string_items(left) + _flatten_string_items(params)
        right_tokens = (
            _flatten_string_items(right)
            + _flatten_string_items(command)
            + _flatten_string_items(data)
        )
        built_message = self.build_composed_message(
            raw=raw,
            left=left_tokens if left_tokens else None,
            right=right_tokens if right_tokens else None,
            separator=self.separator,
            left_join=left_join,
            right_join=right_join,
        )
        merged = self._merge_endpoint_defaults(
            self.MESSAGE_ALLOWED_DEFAULTS,
            _merge_dicts({"message": built_message}, request_params),
        )
        return self.build_url("sendmessage", **merged)

    def intent_url(self, intent: str, **params: Any) -> str:
        merged = self._merge_endpoint_defaults(
            self.INTENT_ALLOWED_DEFAULTS,
            _merge_dicts({"intent": intent}, params),
        )
        return self.build_url("sendintent", **merged)


NOTIFY_OPTION_SPECS: list[tuple[str, str, dict[str, Any]]] = [
    ("--title", "title", {"help": "Notification title."}),
    ("--text", "text", {"help": "Notification text."}),
    ("--sound", "sound", {"help": "Notification sound number (1-10)."}),
    ("--vibration", "vibration", {"help": "Vibration pattern."}),
    ("--url", "url", {"help": "URL to open on notification tap."}),
    ("--id", "id", {"help": "Notification id."}),
    ("--action", "action", {"help": "Action on tap."}),
    ("--icon", "icon", {"help": "Notification icon URL."}),
    ("--led", "led", {"help": "LED color."}),
    ("--led-on", "ledon", {"help": "LED on time in milliseconds."}),
    ("--led-off", "ledoff", {"help": "LED off time in milliseconds."}),
    ("--picture", "picture", {"help": "Big picture URL."}),
    ("--message", "message", {"help": "Action on receive."}),
    ("--action1", "action1", {"help": "Action button 1 action."}),
    ("--action1-name", "action1name", {"help": "Action button 1 label."}),
    ("--action1-icon", "action1icon", {"help": "Action button 1 icon."}),
    ("--action2", "action2", {"help": "Action button 2 action."}),
    ("--action2-name", "action2name", {"help": "Action button 2 label."}),
    ("--action2-icon", "action2icon", {"help": "Action button 2 icon."}),
    ("--action3", "action3", {"help": "Action button 3 action."}),
    ("--action3-name", "action3name", {"help": "Action button 3 label."}),
    ("--action3-icon", "action3icon", {"help": "Action button 3 icon."}),
    ("--sender", "sender", {"help": "Act as sender."}),
    (
        "--statusbar-icon",
        "statusbaricon",
        {"help": "Status bar icon identifier."},
    ),
    ("--ticker", "ticker", {"help": "Ticker text."}),
    ("--priority", "priority", {"help": "Priority (-2 to 2)."}),
    ("--number", "number", {"help": "Notification number."}),
    ("--content-info", "contentinfo", {"help": "Content info string."}),
    ("--subtext", "subtext", {"help": "Subtext."}),
    ("--max-progress", "maxprogress", {"help": "Max progress value."}),
    ("--progress", "progress", {"help": "Current progress value."}),
    (
        "--action-on-dismiss",
        "actionondismiss",
        {"help": "Message/action to execute on dismiss."},
    ),
    ("--password", "password", {"help": "Device password."}),
    ("--ttl", "ttl", {"help": "Message validity time in seconds."}),
    (
        "--collapse-key",
        "collapseKey",
        {"help": "Collapse key / message group."},
    ),
    (
        "--share",
        "share",
        {
            "action": "store_true",
            "help": "Show share button(s). Sends share=1.",
        },
    ),
    (
        "--persistent",
        "persistent",
        {
            "action": "store_true",
            "help": "Make the notification persistent.",
        },
    ),
    (
        "--dismiss-on-touch",
        "dismissontouch",
        {
            "action": "store_true",
            "help": "Dismiss notification on touch.",
        },
    ),
    (
        "--indeterminate-progress",
        "indeterminateprogress",
        {
            "action": "store_true",
            "help": "Use indeterminate progress bar.",
        },
    ),
    (
        "--cancel",
        "cancel",
        {
            "action": "store_true",
            "help": "Cancel a notification. Requires --id and is incompatible with any other notification option.",
        },
    ),
]


def _list_device_names(path: str | os.PathLike[str] | None = None) -> list[str]:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    try:
        config = _load_toml_file(config_path)
    except AutoRemoteError:
        return []
    devices = config.get("devices", {})
    if not isinstance(devices, dict):
        return []
    return sorted(str(name) for name in devices.keys())


def _device_completer(prefix: str, parsed_args: argparse.Namespace, **_: Any) -> list[str]:
    path = getattr(parsed_args, "config", None)
    return [name for name in _list_device_names(path) if name.startswith(prefix)]


def _add_notify_options(parser: argparse.ArgumentParser) -> None:
    for option, dest, kwargs in NOTIFY_OPTION_SPECS:
        parser.add_argument(option, dest=dest, **kwargs)


def _ensure_table(parent: dict[str, Any], key: str) -> dict[str, Any]:
    current = parent.get(key)
    if current is None:
        current = {}
        parent[key] = current
    if not isinstance(current, dict):
        raise AutoRemoteConfigError(f"{key!r} must be a table in config TOML.")
    return current


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoremote",
        description="AutoRemote client and CLI.",
    )

    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to TOML config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--device",
        help="Named device from the TOML config.",
    )
    parser.add_argument(
        "--key",
        help="AutoRemote key. Overrides the configured key.",
    )
    parser.add_argument(
        "--base-url",
        help="Override the AutoRemote base URL.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        help="Number of retries on failure.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        help="Delay between retries in seconds.",
    )
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="Print the final URL instead of sending the request.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    notify = subparsers.add_parser("notify", help="Send or cancel a notification.")
    _add_notify_options(notify)

    cancel = subparsers.add_parser(
        "cancel", help="Cancel a notification by id."
    )
    cancel.add_argument("id", help="Notification id to cancel.")

    init = subparsers.add_parser("init", help="Initialize config and add a device key.")
    init.add_argument(
        "--device",
        default="phone",
        help="Device name to initialize (default: phone).",
    )

    message = subparsers.add_parser("message", help="Send a message.")
    message.add_argument(
        "raw_words",
        nargs="*",
        help="Raw message words. These are joined with spaces when no structured options are used.",
    )
    message.add_argument(
        "--raw",
        help="Raw message string.",
    )
    message.add_argument(
        "--left",
        action="append",
        default=[],
        help="Add one token to the left side of the separator. Repeatable.",
    )
    message.add_argument(
        "--right",
        action="append",
        default=[],
        help="Add one token to the right side of the separator. Repeatable.",
    )
    message.add_argument(
        "--param",
        action="append",
        default=[],
        help="Alias for --left. Good for the documented 'param1 param2=:=command' format.",
    )
    message.add_argument(
        "--command",
        action="append",
        default=[],
        help="Append one token to the right side. Good for the documented AutoRemote command part.",
    )
    message.add_argument(
        "--data",
        action="append",
        default=[],
        help="Append one token to the right side. Useful when treating the right side as payload/data.",
    )
    message.add_argument(
        "--left-json",
        action="append",
        default=[],
        help='JSON array of strings to append to the left side, e.g. \'["a","b"]\'. Repeatable.',
    )
    message.add_argument(
        "--right-json",
        action="append",
        default=[],
        help='JSON array of strings to append to the right side. Repeatable.',
    )
    message.add_argument(
        "--param-json",
        action="append",
        default=[],
        help='Alias for --left-json.',
    )
    message.add_argument(
        "--command-json",
        action="append",
        default=[],
        help='JSON array of strings appended to the right side.',
    )
    message.add_argument(
        "--data-json",
        action="append",
        default=[],
        help='JSON array of strings appended to the right side.',
    )
    message.add_argument(
        "--left-join",
        default=" ",
        help="Joiner for left-side tokens (default: space).",
    )
    message.add_argument(
        "--right-join",
        default=" ",
        help="Joiner for right-side tokens (default: space).",
    )
    message.add_argument(
        "--target",
        help="Target field.",
    )
    message.add_argument(
        "--sender",
        help="Act as sender.",
    )
    message.add_argument(
        "--password",
        help="Device password.",
    )
    message.add_argument(
        "--ttl",
        help="Message validity time in seconds.",
    )
    message.add_argument(
        "--collapse-key",
        dest="collapseKey",
        help="Collapse key / message group.",
    )

    intent = subparsers.add_parser("intent", help="Send an intent/URL.")
    intent.add_argument("intent", help="Intent or URL to send.")
    intent.add_argument(
        "--target",
        help="Target field.",
    )
    intent.add_argument(
        "--sender",
        help="Act as sender.",
    )
    intent.add_argument(
        "--password",
        help="Device password.",
    )
    intent.add_argument(
        "--ttl",
        help="Message validity time in seconds.",
    )
    intent.add_argument(
        "--collapse-key",
        dest="collapseKey",
        help="Collapse key / message group.",
    )

    if argcomplete is not None:  # pragma: no branch
        for current in (parser, notify, cancel, message, intent):
            for action in current._actions:  # pylint: disable=protected-access
                if action.dest == "device":
                    action.completer = _device_completer  # type: ignore[attr-defined]

    return parser


def _build_client(args: argparse.Namespace) -> AutoRemote:
    config_path = Path(args.config).expanduser()

    try:
        return AutoRemote.from_config(
            device=args.device,
            path=config_path,
            key=args.key,
            base_url=args.base_url,
            timeout=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    except AutoRemoteConfigError:
        if args.key:
            return AutoRemote(
                key=args.key,
                base_url=args.base_url or DEFAULT_BASE_URL,
                timeout=args.timeout if args.timeout is not None else 5,
                retries=args.retries if args.retries is not None else 0,
                retry_delay=args.retry_delay if args.retry_delay is not None else 1.0,
                config_path=config_path,
                device_name=args.device,
            )
        raise


def _validate_notify_cancel_args(args: argparse.Namespace) -> None:
    if not getattr(args, "cancel", False):
        return
    if not getattr(args, "id", None):
        raise AutoRemoteUsageError("--cancel requires --id.")

    forbidden = []
    for _, dest, _ in NOTIFY_OPTION_SPECS:
        if dest in {"id", "cancel"}:
            continue
        value = getattr(args, dest, None)
        if value not in (None, False):
            forbidden.append(dest)

    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise AutoRemoteUsageError(
            f"--cancel is incompatible with any notification option other than --id. "
            f"Found: {names}"
        )


def _json_tokens(items: Iterable[str], *, option_name: str) -> list[str]:
    result: list[str] = []
    for item in items:
        result.extend(_parse_json_string_array(item, option_name=option_name))
    return result


def _structured_message_requested(args: argparse.Namespace) -> bool:
    return any(
        [
            args.left,
            args.right,
            args.param,
            args.command,
            args.data,
            args.left_json,
            args.right_json,
            args.param_json,
            args.command_json,
            args.data_json,
        ]
    )


def _handle_notify(client: AutoRemote, args: argparse.Namespace) -> str:
    _validate_notify_cancel_args(args)

    params: dict[str, Any] = {}
    for _, dest, _ in NOTIFY_OPTION_SPECS:
        value = getattr(args, dest, None)
        if value is not None and value is not False:
            params[dest] = value

    if args.print_url:
        return client.notify_url(**params)
    return client.notify(**params)


def _handle_cancel(client: AutoRemote, args: argparse.Namespace) -> str:
    if args.print_url:
        return client.notify_url(id=args.id, cancel=True)
    return client.cancel_notification(args.id)


def _handle_message(client: AutoRemote, args: argparse.Namespace) -> str:
    raw_inputs = []
    if args.raw is not None:
        raw_inputs.append(args.raw)
    if args.raw_words:
        raw_inputs.append(" ".join(args.raw_words))

    structured = _structured_message_requested(args)
    if raw_inputs and structured:
        raise AutoRemoteUsageError(
            "Raw message input is incompatible with structured left/right composition."
        )

    if len(raw_inputs) > 1:
        raise AutoRemoteUsageError(
            "Use either positional raw words or --raw, not both."
        )

    raw_value = raw_inputs[0] if raw_inputs else None

    left_tokens = (
        list(args.left)
        + list(args.param)
        + _json_tokens(args.left_json, option_name="--left-json")
        + _json_tokens(args.param_json, option_name="--param-json")
    )
    right_tokens = (
        list(args.right)
        + list(args.command)
        + list(args.data)
        + _json_tokens(args.right_json, option_name="--right-json")
        + _json_tokens(args.command_json, option_name="--command-json")
        + _json_tokens(args.data_json, option_name="--data-json")
    )

    if raw_value is None and not left_tokens and not right_tokens:
        raise AutoRemoteUsageError(
            "No message content was provided. Use raw words / --raw or structured options."
        )

    common_params = {
        key: value
        for key, value in {
            "target": args.target,
            "sender": args.sender,
            "password": args.password,
            "ttl": args.ttl,
            "collapseKey": args.collapseKey,
        }.items()
        if value is not None
    }

    if args.print_url:
        return client.message_url(
            raw=raw_value,
            left=left_tokens if left_tokens else None,
            right=right_tokens if right_tokens else None,
            left_join=args.left_join,
            right_join=args.right_join,
            **common_params,
        )

    return client.message(
        raw=raw_value,
        left=left_tokens if left_tokens else None,
        right=right_tokens if right_tokens else None,
        left_join=args.left_join,
        right_join=args.right_join,
        **common_params,
    )


def _handle_init(args: argparse.Namespace) -> str:
    config_path = Path(args.config).expanduser()
    config = _load_toml_file(config_path)

    if config and not isinstance(config, dict):
        raise AutoRemoteConfigError("Top-level TOML object must be a table.")

    device_name = str(args.device or "phone").strip()
    if not device_name:
        raise AutoRemoteUsageError("Device name cannot be empty.")

    defaults = _ensure_table(config, "defaults")
    defaults.setdefault("timeout", 5)
    defaults.setdefault("retries", 1)
    defaults.setdefault("retry_delay", 1.0)
    defaults.setdefault("ttl", 86400)

    devices = _ensure_table(config, "devices")
    device_cfg = _ensure_table(devices, device_name)

    existing_key = str(device_cfg.get("key", "")).strip()
    if existing_key:
        return (
            f"Device {device_name!r} already has a key in {config_path}. "
            "No changes were made."
        )

    key = input(f"API key for device '{device_name}': ").strip()
    if not key:
        raise AutoRemoteUsageError("API key cannot be empty.")

    device_cfg["key"] = key
    config.setdefault("default_device", device_name)
    _save_toml_file(config_path, config)
    return f"Initialized device {device_name!r} in {config_path}."


def _handle_intent(client: AutoRemote, args: argparse.Namespace) -> str:
    params = {
        key: value
        for key, value in {
            "target": args.target,
            "sender": args.sender,
            "password": args.password,
            "ttl": args.ttl,
            "collapseKey": args.collapseKey,
        }.items()
        if value is not None
    }

    if args.print_url:
        return client.intent_url(args.intent, **params)
    return client.intent(args.intent, **params)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    if argcomplete is not None:  # pragma: no cover
        argcomplete.autocomplete(parser)

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            output = _handle_init(args)
            if output:
                print(output)
            return 0

        client = _build_client(args)

        if args.command == "notify":
            output = _handle_notify(client, args)
        elif args.command == "cancel":
            output = _handle_cancel(client, args)
        elif args.command == "message":
            output = _handle_message(client, args)
        elif args.command == "intent":
            output = _handle_intent(client, args)
        else:  # pragma: no cover
            raise AutoRemoteUsageError(f"Unknown command: {args.command}")

        if output:
            print(output)
        return 0

    except AutoRemoteError as exc:
        print(f"autoremote: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
