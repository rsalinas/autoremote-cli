# autoremote-cli

A lightweight AutoRemote client and CLI as a standard Python package.

## Highlights

- Send notifications, messages, and intents from the command line.
- Use a TOML config with named devices.
- Use the same module as a Python library.
- Interactive `init` command to bootstrap device keys.
- Safe behavior:
  - `notify` rejects empty notifications.
  - API responses are considered successful only when they return `OK`.

## Requirements

- Python 3.11+

## Installation

Install from a checkout in editable mode:

```bash
python -m pip install -U pip
python -m pip install -e .
```

This installs the `autoremote` console command.

You can still run the compatibility script directly from the repository:

```bash
./autoremote.py --help
```

Optional shell completion:

```bash
pip install argcomplete
eval "$(register-python-argcomplete autoremote.py)"
```

## Configuration

Default config path:

- `$XDG_CONFIG_HOME/autoremote/config.toml`
- or `~/.config/autoremote/config.toml` when `XDG_CONFIG_HOME` is not set.

`base_url` and message separator are code constants now, so they do not need to be present in TOML.

### Example config file

```toml
default_device = "phone"

[defaults]
timeout = 5
retries = 1
retry_delay = 1.0
ttl = 86400

[devices.phone]
key = "YOUR_PHONE_KEY"

[devices.tablet]
key = "YOUR_TABLET_KEY"
password = "optional-password"
sender = "phone"
```

## CLI usage

Global options:

```bash
autoremote --help
autoremote --config ~/.config/autoremote/config.toml --device phone <command>

# Legacy compatibility entrypoint (still supported)
./autoremote.py --help
```

### Initialize config interactively

```bash
autoremote init
autoremote --config /tmp/autoremote.toml init --device laptop
```

The command asks for an API key if the selected device has no key yet.

### Send notifications

```bash
autoremote notify --title "Build" --text "Build finished"
autoremote notify --title "Host booted" --text "Server is up" --id "server-up" --ttl 86400
autoremote notify --cancel --id "server-up"
```

`notify` rejects empty payloads, so at least one of `--title`, `--text`, or notification `--message` must be present.

### Send messages

```bash
# Raw message
autoremote message "hello from cli"

# Structured form: left=:=right
autoremote message --param p1 --param p2 --command do_something
```

### Send intents

```bash
autoremote intent "https://example.com"
```

### Print URL instead of sending

```bash
autoremote --print-url notify --title "Preview" --text "No network call"
```

## Python module usage

```python
from autoremote_cli import AutoRemote

# From config file
client = AutoRemote.from_config(device="phone")
client.notify(title="Hello", text="From Python")

# Explicit key
client2 = AutoRemote(key="YOUR_DEVICE_KEY")
client2.message(raw="hello from library")
client2.intent("https://example.com")
```

### Message composition helpers

```python
from autoremote_cli import AutoRemote

msg = AutoRemote.build_composed_message(left=["param1", "param2"], right="command")
# => "param1 param2=:=command"
```

## Development

Install tools:

```bash
python -m pip install -U pip
python -m pip install pytest ruff pre-commit
```

Run checks:

```bash
ruff check .
pytest -q
```

Enable pre-commit hooks:

```bash
pre-commit install
pre-commit run --all-files
```

## CI

GitHub Actions runs:

- Ruff linting
- Pytest test suite
- Python versions: 3.11, 3.12, 3.13

Workflow file: `.github/workflows/ci.yml`
