from __future__ import annotations

import builtins
from pathlib import Path
import tomllib

import pytest

import autoremote
import autoremote_cli


class _DummyResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self) -> "_DummyResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False


def test_package_imports_public_api() -> None:
    assert autoremote_cli.AutoRemote is not None
    assert autoremote_cli.main is not None


def test_request_accepts_ok_response(monkeypatch: pytest.MonkeyPatch) -> None:
    client = autoremote.AutoRemote(key="k")

    def fake_urlopen(url: str, timeout: float):  # noqa: ARG001
        return _DummyResponse("OK")

    monkeypatch.setattr(autoremote.urllib.request, "urlopen", fake_urlopen)
    assert client._request("sendnotification", title="t") == "OK"


def test_request_rejects_non_ok_response(monkeypatch: pytest.MonkeyPatch) -> None:
    client = autoremote.AutoRemote(key="k")

    def fake_urlopen(url: str, timeout: float):  # noqa: ARG001
        return _DummyResponse("NotRegistered")

    monkeypatch.setattr(autoremote.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(autoremote.AutoRemoteError, match="Request failed"):
        client._request("sendnotification", title="t")


def test_notify_rejects_empty_notification() -> None:
    client = autoremote.AutoRemote(key="k")

    with pytest.raises(autoremote.AutoRemoteUsageError, match="Notification cannot be empty"):
        client.notify()


def test_notify_allows_non_empty_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    client = autoremote.AutoRemote(key="k")

    def fake_request(endpoint: str, **params: str) -> str:  # noqa: ARG001
        return "OK"

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.notify(title="Hello") == "OK"


def test_handle_init_creates_config_for_missing_device_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "autoremote.toml"
    parser = autoremote._build_parser()
    args = parser.parse_args(["--config", str(config_path), "init", "--device", "phone"])

    monkeypatch.setattr(builtins, "input", lambda _prompt: "abc-123")

    result = autoremote._handle_init(args)
    assert "Initialized device 'phone'" in result

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_device"] == "phone"
    assert data["defaults"]["timeout"] == 5
    assert data["defaults"]["ttl"] == 86400
    assert data["devices"]["phone"]["key"] == "abc-123"


def test_handle_init_does_not_overwrite_existing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "autoremote.toml"
    config_path.write_text(
        """
default_device = "phone"

[devices.phone]
key = "already-there"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    parser = autoremote._build_parser()
    args = parser.parse_args(["--config", str(config_path), "init", "--device", "phone"])

    def fail_if_called(_prompt: str) -> str:
        raise AssertionError("input() should not be called when key already exists")

    monkeypatch.setattr(builtins, "input", fail_if_called)

    result = autoremote._handle_init(args)
    assert "already has a key" in result

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["devices"]["phone"]["key"] == "already-there"
