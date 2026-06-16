#!/usr/bin/env python3
"""
Rclone Watch Manager
====================

A polished, menu-driven operations console for one-way folder sync watchers that
mirror local folders to Google Drive using Docker, rclone, and Linux inotify.

Major features
--------------
- First-run setup wizard
- Colorized terminal UI with graceful no-color fallback
- Health dashboard
- Settings editor
- Shared rclone configuration/token for all watchers
- One reusable Docker image for all watcher containers
- One container per watched folder
- Add / edit / remove watcher entries
- Start / stop one watcher or all watchers
- Remote test/check menu
- Built-in log viewer + live log follow
- Log parsing for last event / transfer / failure timestamps
- Persistent container status cache
- Dry-run validation/test before starting watchers
- Backup/export and restore for registry, settings, generated compose files,
  rclone config, template files, and status cache
- systemd unit generation for host-managed boot behavior

Design philosophy
-----------------
This tool favors boring, explicit, recoverable behavior:

- Local registry is the source of truth for watched folders.
- All watchers use one shared rclone config directory.
- Removing a watcher does not delete local data or Google Drive data.
- The user is warned before risky restore or overwrite operations.
- Start operations validate prerequisites first.

Runtime note
------------
The watcher container uses inotify, which is Linux-kernel functionality. For
reliable change detection, run the watcher containers on the Linux host where the
filesystem changes actually occur.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# =============================================================================
# Global configuration
# =============================================================================

APP_NAME = "rclone-watch-manager"
APP_DISPLAY_NAME = "Rclone Watch Manager"
APP_VERSION = "1.0.1"
APP_ROOT = Path(os.environ.get("RWM_APP_ROOT", str(Path.home() / f".{APP_NAME}"))).resolve()
REGISTRY_PATH = APP_ROOT / "registry.json"
SETTINGS_PATH = APP_ROOT / "settings.json"
STATUS_CACHE_PATH = APP_ROOT / "status-cache.json"
SHARED_RCLONE_CONFIG_DIR = APP_ROOT / "rclone-config"
TEMPLATE_DIR = APP_ROOT / "template"
STACKS_DIR = APP_ROOT / "stacks"
LOG_DIR = APP_ROOT / "logs"
BACKUP_DIR = APP_ROOT / "backups"
SYSTEMD_DIR = APP_ROOT / "systemd"

IMAGE_NAME = "rclone-watch-manager:latest"
DEFAULT_REMOTE_NAME = "Drive"
DEFAULT_DEBOUNCE_SECONDS = 45
DEFAULT_TIMEZONE = "UTC"
DEFAULT_LOG_TAIL = 150
SUPPORTED_RUNTIME_OS = {"Linux"}


# =============================================================================
# Embedded Docker template assets
# =============================================================================

DOCKERFILE_CONTENT = """\
FROM rclone/rclone:latest
RUN apk add --no-cache inotify-tools bash tzdata
COPY watch.sh /watch.sh
RUN chmod +x /watch.sh
ENTRYPOINT ["/watch.sh"]
"""

WATCH_SH_CONTENT = """\
#!/usr/bin/env bash
set -euo pipefail

WATCH_DIR="${WATCH_DIR:-/data/to-sync}"
REMOTE_PATH="${REMOTE_PATH:-Drive:Documents}"
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-45}"
SYNC_MODE="${SYNC_MODE:-sync}"
DRY_RUN_ON_START="${DRY_RUN_ON_START:-false}"

# SYNC_MODE:
#   sync = mirror source to remote, including deletions
#   copy = upload/update only, never delete remote files
#
# DRY_RUN_ON_START:
#   true = perform a dry-run transfer test before entering watch mode
#   false = skip startup dry-run

echo "[watch] starting at $(date -Is)"
echo "[watch] watching: ${WATCH_DIR}"
echo "[watch] remote:   ${REMOTE_PATH}"
echo "[watch] debounce: ${DEBOUNCE_SECONDS}s"
echo "[watch] mode:     ${SYNC_MODE}"
echo "[watch] dry-run-on-start: ${DRY_RUN_ON_START}"

timer_pid=""

run_rclone() {
  local extra_args=("$@")

  if [[ "${SYNC_MODE}" == "copy" ]]; then
    rclone copy "${WATCH_DIR}" "${REMOTE_PATH}" \
      --fast-list \
      --log-level INFO \
      "${extra_args[@]}"
  else
    rclone sync "${WATCH_DIR}" "${REMOTE_PATH}" \
      --fast-list \
      --delete-during \
      --log-level INFO \
      "${extra_args[@]}"
  fi
}

sync_now() {
  echo "[watch] transfer starting at $(date -Is)"
  if run_rclone; then
    echo "[watch] transfer complete at $(date -Is)"
  else
    echo "[watch] transfer failed at $(date -Is)"
    exit 1
  fi
}

schedule_sync() {
  if [[ -n "${timer_pid}" ]] && kill -0 "${timer_pid}" 2>/dev/null; then
    kill "${timer_pid}" 2>/dev/null || true
  fi

  (
    sleep "${DEBOUNCE_SECONDS}"
    sync_now
  ) &
  timer_pid="$!"
}

if [[ ! -d "${WATCH_DIR}" ]]; then
  echo "[watch] ERROR: watch directory does not exist: ${WATCH_DIR}"
  exit 1
fi

if [[ "${DRY_RUN_ON_START}" == "true" ]]; then
  echo "[watch] startup dry-run beginning at $(date -Is)"
  if run_rclone --dry-run; then
    echo "[watch] startup dry-run complete at $(date -Is)"
  else
    echo "[watch] startup dry-run failed at $(date -Is)"
    exit 1
  fi
fi

inotifywait -m -r -e modify,create,delete,move --format '%T %e %w%f' --timefmt '%F %T' "${WATCH_DIR}" |
while read -r line; do
  echo "[event] $line"
  schedule_sync
done
"""


# =============================================================================
# Terminal colors / UI helpers
# =============================================================================

class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return True


def c(text: str, color: str) -> str:
    if not supports_color():
        return text
    return f"{color}{text}{Color.RESET}"


def icon(status: str) -> str:
    if status in {"ok", "running", "healthy"}:
        return c("●", Color.GREEN)
    if status in {"warn", "degraded", "exited", "not-created"}:
        return c("●", Color.YELLOW)
    if status in {"bad", "dead", "failed", "error", "restarting"}:
        return c("●", Color.RED)
    return c("●", Color.BLUE)


def banner(title: str) -> None:
    print("\n" + c("=" * 88, Color.CYAN))
    print(c(title, Color.BOLD + Color.CYAN))
    print(c("=" * 88, Color.CYAN))


def section(title: str) -> None:
    print("\n" + c(f"-- {title} --", Color.BOLD + Color.MAGENTA))


def info(message: str) -> None:
    print(f"{c('[INFO]', Color.GREEN)} {message}")


def warn(message: str) -> None:
    print(f"{c('[WARN]', Color.YELLOW)} {message}")


def error(message: str) -> None:
    print(f"{c('[ERROR]', Color.RED)} {message}")


def prompt(message: str, default: Optional[str] = None) -> str:
    suffix = c(f" [{default}]", Color.DIM) if default else ""
    value = input(f"{c(message, Color.BOLD)}{suffix}: ").strip()
    return value if value else (default or "")


def prompt_int(message: str, default: int) -> int:
    raw = prompt(message, default=str(default))
    try:
        return int(raw)
    except ValueError:
        warn(f"Invalid integer '{raw}', using {default}.")
        return default


def confirm(message: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{c(message, Color.BOLD)} ({hint}): ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def pause() -> None:
    input(c("\nPress Enter to continue...", Color.DIM))


def wrap(text: str) -> str:
    return textwrap.dedent(text).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sanitize_name(raw: str) -> str:
    cleaned = raw.strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    return cleaned


# =============================================================================
# Data models
# =============================================================================

@dataclass
class Settings:
    timezone: str = DEFAULT_TIMEZONE
    remote_name: str = DEFAULT_REMOTE_NAME
    log_tail_lines: int = DEFAULT_LOG_TAIL
    first_run_complete: bool = False
    dry_run_before_start: bool = True
    default_sync_mode: str = "copy"  # safer default; user can switch to sync
    color_enabled: bool = True
    systemd_user_mode: bool = False


@dataclass
class WatchEntry:
    name: str
    local_path: str
    drive_root_path: str
    remote_name: str = DEFAULT_REMOTE_NAME
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS
    timezone: str = DEFAULT_TIMEZONE
    sync_mode: str = "copy"
    enabled: bool = True

    @property
    def container_name(self) -> str:
        return f"rclone-watch-{self.name}"

    @property
    def stack_dir(self) -> Path:
        return STACKS_DIR / self.name

    @property
    def compose_file(self) -> Path:
        return self.stack_dir / "docker-compose.yml"


@dataclass
class StatusRecord:
    name: str
    container_name: str
    last_checked: str = ""
    container_status: str = "unknown"
    last_event: str = ""
    last_transfer_start: str = ""
    last_transfer_complete: str = ""
    last_transfer_failed: str = ""
    last_error: str = ""
    dry_run_ok: Optional[bool] = None
    remote_ok: Optional[bool] = None


# =============================================================================
# Subprocess helpers
# =============================================================================

def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(
    cmd: List[str],
    *,
    check: bool = False,
    capture_output: bool = True,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        check=check,
        capture_output=capture_output,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def get_platform_summary() -> Tuple[str, str]:
    return platform.system(), platform.machine()


def detect_linux_package_manager() -> Optional[str]:
    for candidate in ["apt-get", "dnf", "yum", "pacman", "zypper"]:
        if command_exists(candidate):
            return candidate
    return None


# =============================================================================
# File layout, settings, registry, status cache
# =============================================================================

def save_settings(settings: Settings) -> None:
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def ensure_base_layout() -> None:
    for path in [APP_ROOT, SHARED_RCLONE_CONFIG_DIR, TEMPLATE_DIR, STACKS_DIR, LOG_DIR, BACKUP_DIR, SYSTEMD_DIR]:
        path.mkdir(parents=True, exist_ok=True)

    dockerfile_path = TEMPLATE_DIR / "Dockerfile"
    watch_script_path = TEMPLATE_DIR / "watch.sh"

    if not dockerfile_path.exists():
        dockerfile_path.write_text(DOCKERFILE_CONTENT, encoding="utf-8")
    if not watch_script_path.exists():
        watch_script_path.write_text(WATCH_SH_CONTENT, encoding="utf-8")
        watch_script_path.chmod(0o755)

    if not REGISTRY_PATH.exists():
        REGISTRY_PATH.write_text(json.dumps({"entries": []}, indent=2), encoding="utf-8")
    if not STATUS_CACHE_PATH.exists():
        STATUS_CACHE_PATH.write_text(json.dumps({"records": {}}, indent=2), encoding="utf-8")
    if not SETTINGS_PATH.exists():
        save_settings(Settings())


def load_settings() -> Settings:
    ensure_base_layout()
    raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return Settings(**{**asdict(Settings()), **raw})


def load_registry() -> List[WatchEntry]:
    ensure_base_layout()
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    entries = []
    for item in raw.get("entries", []):
        # Merge defaults so older registry files survive schema upgrades.
        entries.append(WatchEntry(**{**asdict(WatchEntry(name="tmp", local_path="/tmp", drive_root_path="tmp")), **item}))
    return entries


def save_registry(entries: List[WatchEntry]) -> None:
    REGISTRY_PATH.write_text(json.dumps({"entries": [asdict(entry) for entry in entries]}, indent=2), encoding="utf-8")


def load_status_cache() -> Dict[str, StatusRecord]:
    ensure_base_layout()
    raw = json.loads(STATUS_CACHE_PATH.read_text(encoding="utf-8"))
    records = {}
    for name, payload in raw.get("records", {}).items():
        records[name] = StatusRecord(**{**asdict(StatusRecord(name=name, container_name="")), **payload})
    return records


def save_status_cache(records: Dict[str, StatusRecord]) -> None:
    STATUS_CACHE_PATH.write_text(json.dumps({"records": {k: asdict(v) for k, v in records.items()}}, indent=2), encoding="utf-8")


def find_entry(entries: List[WatchEntry], name: str) -> Optional[WatchEntry]:
    for entry in entries:
        if entry.name == name:
            return entry
    return None


# =============================================================================
# Environment checks
# =============================================================================

def docker_installed() -> bool:
    return command_exists("docker")


def docker_ready() -> bool:
    if not docker_installed():
        return False
    return run_command(["docker", "info"], check=False).returncode == 0


def docker_compose_available() -> bool:
    if not docker_installed():
        return False
    return run_command(["docker", "compose", "version"], check=False).returncode == 0


def rclone_installed() -> bool:
    return command_exists("rclone")


def rclone_config_file() -> Path:
    return SHARED_RCLONE_CONFIG_DIR / "rclone.conf"


def rclone_configured() -> bool:
    path = rclone_config_file()
    return path.exists() and path.stat().st_size > 0


def shared_image_exists() -> bool:
    if not docker_ready():
        return False
    return run_command(["docker", "image", "inspect", IMAGE_NAME], check=False).returncode == 0


def container_status(entry: WatchEntry) -> str:
    if not docker_ready():
        return "docker-unavailable"
    result = run_command(["docker", "inspect", "-f", "{{.State.Status}}", entry.container_name], check=False)
    if result.returncode != 0:
        return "not-created"
    return (result.stdout or "unknown").strip() or "unknown"


# =============================================================================
# Install flows
# =============================================================================

def install_docker() -> None:
    if docker_installed():
        info("Docker already appears to be installed.")
        return

    banner("Install Docker")
    system, _ = get_platform_summary()

    if system == "Linux":
        print(wrap("""
            Choose an installation method:
              1) Docker convenience script: curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
              2) Distribution package manager
              0) Cancel

            Note: Docker labels the convenience script as a convenience/testing path,
            not the preferred production install method.
        """))
        choice = prompt("Choose", default="1")
        if choice == "1":
            if confirm("Run Docker's convenience script now?", default=False):
                subprocess.run(["sh", "-c", "curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh"], check=False)
                warn("You may need: sudo systemctl enable --now docker && sudo usermod -aG docker $USER")
            return
        if choice == "2":
            manager = detect_linux_package_manager()
            if manager == "apt-get":
                subprocess.run(["sudo", "apt-get", "update"], check=False)
                subprocess.run(["sudo", "apt-get", "install", "-y", "docker.io", "docker-compose-plugin"], check=False)
                subprocess.run(["sudo", "systemctl", "enable", "--now", "docker"], check=False)
                return
            if manager in {"dnf", "yum"}:
                subprocess.run(["sudo", manager, "install", "-y", "docker", "docker-compose-plugin"], check=False)
                subprocess.run(["sudo", "systemctl", "enable", "--now", "docker"], check=False)
                return
            if manager == "pacman":
                subprocess.run(["sudo", "pacman", "-Syu", "docker", "docker-compose", "--noconfirm"], check=False)
                subprocess.run(["sudo", "systemctl", "enable", "--now", "docker"], check=False)
                return
            warn("No supported package manager was detected.")
            return

    if system == "Darwin" and command_exists("brew"):
        if confirm("Install Docker Desktop via Homebrew cask?", default=False):
            subprocess.run(["brew", "install", "--cask", "docker"], check=False)
        return

    warn("Automatic Docker install is not supported on this platform. Install Docker manually.")


def install_rclone() -> None:
    if rclone_installed():
        info("rclone already appears to be installed.")
        return

    banner("Install rclone")
    system, _ = get_platform_summary()

    if system in {"Linux", "Darwin"}:
        print(wrap("""
            Choose an installation method:
              1) Official rclone install script: sudo -v ; curl https://rclone.org/install.sh | sudo bash
              2) Package manager / Homebrew
              0) Cancel
        """))
        choice = prompt("Choose", default="1")
        if choice == "1":
            if confirm("Run the official rclone install script now?", default=False):
                subprocess.run(["sh", "-c", "sudo -v ; curl https://rclone.org/install.sh | sudo bash"], check=False)
            return
        if choice == "2":
            if system == "Darwin" and command_exists("brew"):
                subprocess.run(["brew", "install", "rclone"], check=False)
                return
            manager = detect_linux_package_manager()
            if manager == "apt-get":
                subprocess.run(["sudo", "apt-get", "update"], check=False)
                subprocess.run(["sudo", "apt-get", "install", "-y", "rclone"], check=False)
                return
            if manager in {"dnf", "yum"}:
                subprocess.run(["sudo", manager, "install", "-y", "rclone"], check=False)
                return
            if manager == "pacman":
                subprocess.run(["sudo", "pacman", "-Syu", "rclone", "--noconfirm"], check=False)
                return

    warn("Automatic rclone install is not supported here. Install rclone manually.")


# =============================================================================
# rclone remote setup and checks
# =============================================================================

def rclone_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["RCLONE_CONFIG"] = str(rclone_config_file())
    return env


def setup_rclone_configuration(settings: Settings) -> None:
    if not rclone_installed():
        error("rclone is not installed. Install it first.")
        return
    banner("rclone configuration setup")
    print(wrap(f"""
        Shared config file:
          {rclone_config_file()}

        This config is mounted into every watcher container.
        Recommended remote name:
          {settings.remote_name}
    """))
    subprocess.run(["rclone", "config"], env=rclone_env(), check=False)
    if rclone_configured():
        info("Shared rclone config is present.")
    else:
        warn("Shared rclone config was not detected after setup.")


def list_rclone_remotes() -> List[str]:
    if not rclone_installed() or not rclone_configured():
        return []
    result = run_command(["rclone", "listremotes"], env=rclone_env(), check=False)
    if result.returncode != 0:
        return []
    return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]


def test_rclone_remote(remote_name: str) -> bool:
    if not rclone_installed() or not rclone_configured():
        error("rclone is not installed or not configured.")
        return False
    result = run_command(["rclone", "lsd", f"{remote_name}:"], env=rclone_env(), check=False)
    if result.returncode == 0:
        info(f"Remote reachable: {remote_name}:")
        if result.stdout.strip():
            print(result.stdout.strip())
        return True
    error("Remote test failed.")
    print((result.stderr or result.stdout).strip())
    return False


def remote_check_menu(settings: Settings) -> None:
    while True:
        banner("Remote test / check")
        remotes = list_rclone_remotes()
        if remotes:
            print(c("Configured remotes:", Color.BOLD))
            for idx, remote in enumerate(remotes, start=1):
                print(f"  {idx}) {remote}")
        else:
            warn("No remotes detected.")
        print(wrap("""
            1) Test a remote
            2) Run rclone config
            0) Back
        """))
        choice = prompt("Choose", default="1")
        if choice == "0":
            return
        if choice == "2":
            setup_rclone_configuration(settings)
            continue
        if choice == "1":
            raw = prompt("Remote number or name", default=settings.remote_name)
            chosen = raw
            if raw.isdigit() and remotes:
                try:
                    chosen = remotes[int(raw) - 1]
                except (ValueError, IndexError):
                    error("Invalid remote selection.")
                    continue
            test_rclone_remote(chosen)
            continue
        warn("Unknown choice.")


# =============================================================================
# Image and compose generation
# =============================================================================

def build_base_image_if_needed(force: bool = False) -> bool:
    ensure_base_layout()
    if not docker_ready() or not docker_compose_available():
        error("Docker and docker compose must be working before building the image.")
        return False
    if not force and shared_image_exists():
        return True
    banner("Building shared watcher image")
    result = subprocess.run(["docker", "build", "-t", IMAGE_NAME, str(TEMPLATE_DIR)], check=False)
    if result.returncode != 0:
        error("Failed to build watcher image.")
        return False
    info(f"Built image: {IMAGE_NAME}")
    return True


def compose_yaml_for_entry(entry: WatchEntry, settings: Settings) -> str:
    dry_run = "true" if settings.dry_run_before_start else "false"
    return textwrap.dedent(
        f"""
        services:
          {entry.container_name}:
            image: {IMAGE_NAME}
            container_name: {entry.container_name}
            restart: unless-stopped
            environment:
              TZ: {entry.timezone}
              WATCH_DIR: /data/to-sync
              REMOTE_PATH: {entry.remote_name}:{entry.drive_root_path}
              DEBOUNCE_SECONDS: "{entry.debounce_seconds}"
              SYNC_MODE: {entry.sync_mode}
              DRY_RUN_ON_START: "{dry_run}"
            volumes:
              - {SHARED_RCLONE_CONFIG_DIR}:/config/rclone
              - {entry.local_path}:/data/to-sync
        """
    ).strip() + "\n"


def write_entry_stack(entry: WatchEntry, settings: Settings) -> None:
    entry.stack_dir.mkdir(parents=True, exist_ok=True)
    entry.compose_file.write_text(compose_yaml_for_entry(entry, settings), encoding="utf-8")


# =============================================================================
# Log parsing and status cache
# =============================================================================

def get_container_logs(entry: WatchEntry, tail: int = 300) -> str:
    if not docker_ready():
        return ""
    result = run_command(["docker", "logs", "--tail", str(tail), entry.container_name], check=False)
    return (result.stdout or "") + (result.stderr or "")


def parse_logs_for_status(entry: WatchEntry, logs: str) -> StatusRecord:
    record = StatusRecord(name=entry.name, container_name=entry.container_name)
    record.last_checked = now_iso()
    record.container_status = container_status(entry)

    for line in logs.splitlines():
        if line.startswith("[event]"):
            record.last_event = line
        if "transfer starting" in line:
            record.last_transfer_start = line
        if "transfer complete" in line:
            record.last_transfer_complete = line
        if "transfer failed" in line or "ERROR" in line or "Failed" in line or "CRITICAL" in line:
            if "transfer failed" in line:
                record.last_transfer_failed = line
            record.last_error = line
        if "startup dry-run complete" in line:
            record.dry_run_ok = True
        if "startup dry-run failed" in line:
            record.dry_run_ok = False

    return record


def refresh_status_cache(entries: List[WatchEntry]) -> Dict[str, StatusRecord]:
    records = load_status_cache()
    for entry in entries:
        logs = get_container_logs(entry, tail=500)
        records[entry.name] = parse_logs_for_status(entry, logs)
    save_status_cache(records)
    return records


# =============================================================================
# Dry-run tests
# =============================================================================

def local_dry_run(entry: WatchEntry) -> bool:
    """Run an rclone dry-run locally using the shared rclone config."""
    if not rclone_installed() or not rclone_configured():
        error("rclone is not installed or not configured.")
        return False
    if not Path(entry.local_path).is_dir():
        error(f"Local path missing: {entry.local_path}")
        return False
    command = "copy" if entry.sync_mode == "copy" else "sync"
    cmd = ["rclone", command, entry.local_path, f"{entry.remote_name}:{entry.drive_root_path}", "--dry-run", "--fast-list", "--log-level", "INFO"]
    if command == "sync":
        cmd.insert(4, "--delete-during")
    banner(f"Dry-run test: {entry.name}")
    result = subprocess.run(cmd, env=rclone_env(), check=False)
    ok = result.returncode == 0
    records = load_status_cache()
    rec = records.get(entry.name, StatusRecord(name=entry.name, container_name=entry.container_name))
    rec.last_checked = now_iso()
    rec.dry_run_ok = ok
    records[entry.name] = rec
    save_status_cache(records)
    return ok


# =============================================================================
# Container lifecycle
# =============================================================================

def compose_up(entry: WatchEntry, settings: Settings) -> bool:
    if not docker_ready() or not docker_compose_available():
        error("Docker is not ready. Check `docker info` and `docker compose version`.")
        return False
    if not rclone_configured():
        error("Shared rclone config is missing. Run rclone config first.")
        return False
    if not Path(entry.local_path).is_dir():
        error(f"Local folder is missing: {entry.local_path}")
        return False
    if settings.dry_run_before_start:
        if not local_dry_run(entry):
            error("Dry-run failed. Start aborted.")
            return False
    if not build_base_image_if_needed(force=False):
        return False
    write_entry_stack(entry, settings)
    result = subprocess.run(["docker", "compose", "-f", str(entry.compose_file), "up", "-d"], cwd=str(entry.stack_dir), check=False)
    refresh_status_cache([entry])
    return result.returncode == 0


def compose_stop(entry: WatchEntry) -> bool:
    if not entry.compose_file.exists():
        warn(f"Compose file not found for '{entry.name}'. Nothing to stop.")
        return True
    result = subprocess.run(["docker", "compose", "-f", str(entry.compose_file), "stop"], cwd=str(entry.stack_dir), check=False)
    refresh_status_cache([entry])
    return result.returncode == 0


def compose_down(entry: WatchEntry) -> bool:
    if not entry.compose_file.exists():
        return True
    result = subprocess.run(["docker", "compose", "-f", str(entry.compose_file), "down"], cwd=str(entry.stack_dir), check=False)
    refresh_status_cache([entry])
    return result.returncode == 0


# =============================================================================
# Watcher CRUD
# =============================================================================

def validate_local_folder(path_str: str) -> Tuple[bool, Path]:
    path = Path(path_str).expanduser().resolve()
    return path.exists() and path.is_dir(), path


def missing_folder_flow(path: Path) -> Tuple[str, Optional[Path]]:
    banner("Folder does not exist")
    print(wrap(f"""
        Missing local folder:
          {path}

        Choose:
          1) Create it automatically now
          2) Enter a different path
          3) Cancel
    """))
    while True:
        choice = prompt("Choose", default="2")
        if choice == "1":
            try:
                path.mkdir(parents=True, exist_ok=True)
                info(f"Created folder: {path}")
                return "created", path
            except OSError as exc:
                error(f"Failed to create folder: {exc}")
                return "retry", None
        if choice == "2":
            return "retry", None
        if choice == "3":
            return "cancel", None
        warn("Please choose 1, 2, or 3.")


def choose_sync_mode(default: str = "copy") -> str:
    print(wrap("""
        Sync mode:
          1) copy  - upload/update only, never delete remote files  [safest]
          2) sync  - mirror source to remote, including deletions
    """))
    raw = prompt("Choose sync mode", default="1" if default == "copy" else "2")
    return "sync" if raw == "2" else "copy"


def add_watch_folder(entries: List[WatchEntry], settings: Settings) -> List[WatchEntry]:
    banner("Add watched folder")
    while True:
        name = sanitize_name(prompt("Watcher name (documents, photos, etc.)"))
        if not name:
            error("Name cannot be empty.")
            continue
        if find_entry(entries, name):
            error("A watcher with that name already exists.")
            continue
        break

    while True:
        raw_path = prompt("Local folder path to watch")
        exists, local_path = validate_local_folder(raw_path)
        if exists:
            break
        action, _ = missing_folder_flow(local_path)
        if action == "created":
            exists, local_path = validate_local_folder(str(local_path))
            if exists:
                break
        if action == "cancel":
            warn("Add cancelled.")
            return entries

    drive_path = prompt("Google Drive destination path from remote root", default=local_path.name).strip("/")
    remote_name = prompt("rclone remote name", default=settings.remote_name).strip() or settings.remote_name
    debounce = max(1, prompt_int("Debounce seconds", DEFAULT_DEBOUNCE_SECONDS))
    timezone_value = prompt("Timezone", default=settings.timezone) or settings.timezone
    sync_mode = choose_sync_mode(default=settings.default_sync_mode)

    entry = WatchEntry(
        name=name,
        local_path=str(local_path),
        drive_root_path=drive_path,
        remote_name=remote_name,
        debounce_seconds=debounce,
        timezone=timezone_value,
        sync_mode=sync_mode,
    )
    write_entry_stack(entry, settings)
    entries.append(entry)
    save_registry(entries)
    info(f"Added watcher: {entry.name}")
    if confirm("Run a dry-run test now?", default=True):
        local_dry_run(entry)
    return entries


def select_entry(entries: List[WatchEntry]) -> Optional[WatchEntry]:
    if not entries:
        warn("No watcher entries exist yet.")
        return None
    for idx, entry in enumerate(entries, start=1):
        print(f"{idx}) {entry.name} -> {entry.remote_name}:{entry.drive_root_path}")
    selection = prompt("Enter watcher number")
    try:
        return entries[int(selection) - 1]
    except (ValueError, IndexError):
        error("Invalid selection.")
        return None


def edit_watch_folder(entries: List[WatchEntry], settings: Settings) -> List[WatchEntry]:
    banner("Edit watcher")
    entry = select_entry(entries)
    if not entry:
        return entries

    print(c("Leave blank to keep current value.", Color.DIM))
    new_name = sanitize_name(prompt("Watcher name", default=entry.name))
    if new_name and new_name != entry.name:
        if find_entry(entries, new_name):
            error("Another watcher already uses that name.")
            return entries
        old_stack_dir = entry.stack_dir
        entry.name = new_name
        if old_stack_dir.exists() and old_stack_dir != entry.stack_dir:
            shutil.move(str(old_stack_dir), str(entry.stack_dir))

    while True:
        new_path = prompt("Local folder path", default=entry.local_path)
        exists, resolved = validate_local_folder(new_path)
        if exists:
            entry.local_path = str(resolved)
            break
        action, _ = missing_folder_flow(Path(new_path).expanduser().resolve())
        if action == "cancel":
            warn("Edit cancelled.")
            return entries
        if action == "created":
            exists, resolved = validate_local_folder(new_path)
            if exists:
                entry.local_path = str(resolved)
                break

    entry.drive_root_path = prompt("Google Drive destination path", default=entry.drive_root_path).strip("/") or entry.drive_root_path
    entry.remote_name = prompt("rclone remote name", default=entry.remote_name) or entry.remote_name
    entry.debounce_seconds = max(1, prompt_int("Debounce seconds", entry.debounce_seconds))
    entry.timezone = prompt("Timezone", default=entry.timezone) or entry.timezone
    entry.sync_mode = choose_sync_mode(default=entry.sync_mode)

    write_entry_stack(entry, settings)
    save_registry(entries)
    info("Watcher updated.")
    if container_status(entry) == "running" and confirm("Restart watcher to apply changes?", default=True):
        compose_down(entry)
        compose_up(entry, settings)
    return entries


def remove_watch_folder(entries: List[WatchEntry]) -> List[WatchEntry]:
    banner("Remove watcher")
    entry = select_entry(entries)
    if not entry:
        return entries
    print(wrap(f"""
        This will stop and unregister watcher '{entry.name}'.

        It will NOT delete:
          - local files
          - Google Drive files
          - shared rclone config
    """))
    if not confirm("Continue removal?", default=False):
        info("Removal cancelled.")
        return entries
    compose_stop(entry)
    if confirm("Remove generated compose stack directory too?", default=True):
        shutil.rmtree(entry.stack_dir, ignore_errors=True)
    entries.remove(entry)
    save_registry(entries)
    records = load_status_cache()
    records.pop(entry.name, None)
    save_status_cache(records)
    info("Watcher removed from registry.")
    return entries


# =============================================================================
# Dashboard, logs, status control
# =============================================================================

def health_score(entries: List[WatchEntry], settings: Settings) -> Tuple[int, List[str]]:
    issues: List[str] = []
    score = 100
    if not docker_ready():
        score -= 25
        issues.append("Docker is not reachable")
    if not docker_compose_available():
        score -= 15
        issues.append("Docker Compose is unavailable")
    if not rclone_configured():
        score -= 25
        issues.append("Shared rclone config missing")
    if not shared_image_exists():
        score -= 10
        issues.append("Shared watcher image missing")
    for entry in entries:
        if not Path(entry.local_path).is_dir():
            score -= 5
            issues.append(f"Missing local path for {entry.name}")
        status = container_status(entry)
        if status in {"dead", "restarting"}:
            score -= 10
            issues.append(f"Container {entry.name} status is {status}")
    return max(0, score), issues


def show_dashboard(entries: List[WatchEntry], settings: Settings) -> None:
    records = refresh_status_cache(entries) if docker_ready() else load_status_cache()
    score, issues = health_score(entries, settings)
    banner("Health dashboard")
    score_color = Color.GREEN if score >= 85 else Color.YELLOW if score >= 60 else Color.RED
    print(f"Health score: {c(str(score) + '/100', score_color)}")
    print(f"Watchers:     {len(entries)}")
    print(f"Docker:       {icon('ok' if docker_ready() else 'bad')} {'ready' if docker_ready() else 'not ready'}")
    print(f"rclone cfg:   {icon('ok' if rclone_configured() else 'bad')} {rclone_config_file()}")
    print(f"Image:        {icon('ok' if shared_image_exists() else 'warn')} {IMAGE_NAME}")

    if issues:
        section("Issues")
        for item in issues:
            print(f"- {c(item, Color.YELLOW)}")

    section("Watcher summary")
    if not entries:
        print("No watchers configured.")
        return
    header = f"{'Name':<22} {'Status':<16} {'Mode':<6} {'DryRun':<8} {'Last Transfer'}"
    print(c(header, Color.BOLD))
    print("-" * len(header))
    for entry in entries:
        status = container_status(entry)
        rec = records.get(entry.name)
        dry = "?" if not rec or rec.dry_run_ok is None else "ok" if rec.dry_run_ok else "fail"
        last = rec.last_transfer_complete if rec and rec.last_transfer_complete else "-"
        status_color = Color.GREEN if status == "running" else Color.YELLOW if status in {"exited", "not-created"} else Color.RED
        print(f"{entry.name:<22} {c(status, status_color):<25} {entry.sync_mode:<6} {dry:<8} {last}")


def print_watcher_table(entries: List[WatchEntry]) -> None:
    banner("Watchers")
    if not entries:
        print("No watchers configured.")
        return
    header = f"{'#':<4} {'Name':<22} {'Status':<16} {'Mode':<6} {'Remote'}"
    print(c(header, Color.BOLD))
    print("-" * len(header))
    for idx, entry in enumerate(entries, start=1):
        status = container_status(entry)
        destination = f"{entry.remote_name}:{entry.drive_root_path}"
        color = Color.GREEN if status == "running" else Color.YELLOW if status in {"exited", "not-created"} else Color.RED
        print(f"{idx:<4} {entry.name:<22} {c(status, color):<25} {entry.sync_mode:<6} {destination}")


def show_logs(entry: WatchEntry, settings: Settings) -> None:
    banner(f"Logs: {entry.name}")
    tail = prompt_int("How many lines", settings.log_tail_lines)
    subprocess.run(["docker", "logs", "--tail", str(tail), entry.container_name], check=False)


def follow_logs(entry: WatchEntry) -> None:
    banner(f"Following logs: {entry.name}")
    print(c("Press Ctrl+C to stop following logs.", Color.DIM))
    try:
        subprocess.run(["docker", "logs", "-f", entry.container_name], check=False)
    except KeyboardInterrupt:
        print("\nStopped following logs.")


def log_analysis_menu(entries: List[WatchEntry]) -> None:
    banner("Log analysis")
    if not entries:
        warn("No watchers configured.")
        return
    refresh_status_cache(entries)
    records = load_status_cache()
    for entry in entries:
        rec = records.get(entry.name)
        print(c(f"\n{entry.name}", Color.BOLD))
        if not rec:
            print("  No cached log data.")
            continue
        print(f"  status:              {rec.container_status}")
        print(f"  last checked:        {rec.last_checked or '-'}")
        print(f"  last event:          {rec.last_event or '-'}")
        print(f"  last transfer start: {rec.last_transfer_start or '-'}")
        print(f"  last transfer done:  {rec.last_transfer_complete or '-'}")
        print(f"  last transfer fail:  {rec.last_transfer_failed or '-'}")
        print(f"  last error:          {rec.last_error or '-'}")


def start_all(entries: List[WatchEntry], settings: Settings) -> None:
    for entry in entries:
        if compose_up(entry, settings):
            info(f"Started: {entry.name}")
        else:
            error(f"Failed to start: {entry.name}")


def stop_all(entries: List[WatchEntry]) -> None:
    for entry in entries:
        if compose_stop(entry):
            info(f"Stopped: {entry.name}")
        else:
            error(f"Failed to stop: {entry.name}")


def status_control_menu(entries: List[WatchEntry], settings: Settings) -> List[WatchEntry]:
    while True:
        print_watcher_table(entries)
        print(wrap("""
            1) Start one watcher
            2) Stop one watcher
            3) Show logs for one watcher
            4) Follow logs for one watcher
            5) Start all watchers
            6) Stop all watchers
            7) Run dry-run test for one watcher
            8) Run dry-run tests for all watchers
            9) Analyze parsed logs
            0) Back
        """))
        choice = prompt("Choose", default="0")
        if choice == "0":
            return entries
        if choice == "5":
            start_all(entries, settings)
            continue
        if choice == "6":
            stop_all(entries)
            continue
        if choice == "8":
            for entry in entries:
                local_dry_run(entry)
            continue
        if choice == "9":
            log_analysis_menu(entries)
            pause()
            continue
        entry = select_entry(entries)
        if not entry:
            continue
        if choice == "1":
            compose_up(entry, settings)
        elif choice == "2":
            compose_stop(entry)
        elif choice == "3":
            show_logs(entry, settings)
        elif choice == "4":
            follow_logs(entry)
        elif choice == "7":
            local_dry_run(entry)
        else:
            warn("Unknown choice.")


# =============================================================================
# Settings editor
# =============================================================================

def settings_editor(settings: Settings) -> Settings:
    while True:
        banner("Settings editor")
        print(f"1) Default timezone:          {settings.timezone}")
        print(f"2) Default rclone remote:     {settings.remote_name}")
        print(f"3) Log tail lines:            {settings.log_tail_lines}")
        print(f"4) Dry-run before start:      {settings.dry_run_before_start}")
        print(f"5) Default sync mode:         {settings.default_sync_mode}")
        print(f"6) Color enabled:             {settings.color_enabled}")
        print(f"7) systemd user mode:         {settings.systemd_user_mode}")
        print("0) Back")
        choice = prompt("Choose setting")
        if choice == "0":
            save_settings(settings)
            return settings
        if choice == "1":
            settings.timezone = prompt("Timezone", default=settings.timezone)
        elif choice == "2":
            settings.remote_name = prompt("Remote name", default=settings.remote_name)
        elif choice == "3":
            settings.log_tail_lines = max(10, prompt_int("Log tail lines", settings.log_tail_lines))
        elif choice == "4":
            settings.dry_run_before_start = confirm("Enable dry-run before starting containers?", default=settings.dry_run_before_start)
        elif choice == "5":
            settings.default_sync_mode = choose_sync_mode(default=settings.default_sync_mode)
        elif choice == "6":
            settings.color_enabled = confirm("Enable color output?", default=settings.color_enabled)
        elif choice == "7":
            settings.systemd_user_mode = confirm("Generate systemd user units instead of system units?", default=settings.systemd_user_mode)
        else:
            warn("Unknown setting.")
        save_settings(settings)


# =============================================================================
# Backup / restore
# =============================================================================

def safe_extract_tar(tar: tarfile.TarFile, path: Path) -> None:
    """Prevent path traversal during tar extraction."""
    destination = path.resolve()

    for member in tar.getmembers():
        member_path = (destination / member.name).resolve()
        if destination != member_path and destination not in member_path.parents:
            raise ValueError(f"Unsafe path detected in archive: {member.name}")

    tar.extractall(destination)


def create_backup() -> Optional[Path]:
    ensure_base_layout()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"rclone-watch-manager-backup-{timestamp}.tar.gz"
    banner(f"{APP_DISPLAY_NAME} - Backup & Export")
    print(wrap(f"""
        Backup will include:
          - registry.json
          - settings.json
          - status-cache.json
          - shared rclone config directory
          - generated watcher stacks
          - Docker template files

        Backup path:
          {backup_path}
    """))
    if not confirm("Create this backup?", default=True):
        return None
    with tarfile.open(backup_path, "w:gz") as tar:
        for path in [REGISTRY_PATH, SETTINGS_PATH, STATUS_CACHE_PATH, SHARED_RCLONE_CONFIG_DIR, TEMPLATE_DIR, STACKS_DIR]:
            if path.exists():
                tar.add(path, arcname=str(path.relative_to(APP_ROOT)))
    info(f"Backup created: {backup_path}")
    return backup_path


def restore_backup() -> None:
    banner(f"{APP_DISPLAY_NAME} - Restore & Import")
    backups = sorted(BACKUP_DIR.glob("*.tar.gz"), reverse=True)
    if not backups:
        warn(f"No backups found in {BACKUP_DIR}")
        manual = prompt("Enter backup path manually or leave blank to cancel")
        if not manual:
            return
        backup_path = Path(manual).expanduser().resolve()
    else:
        for idx, path in enumerate(backups, start=1):
            print(f"{idx}) {path.name}")
        raw = prompt("Choose backup number or path")
        if raw.isdigit():
            try:
                backup_path = backups[int(raw) - 1]
            except (ValueError, IndexError):
                error("Invalid selection.")
                return
        else:
            backup_path = Path(raw).expanduser().resolve()

    if not backup_path.exists():
        error(f"Backup not found: {backup_path}")
        return

    warn("Restore can overwrite local registry/settings/config files.")
    if not confirm("Create a safety backup before restore?", default=True):
        warn("Skipping safety backup by user choice.")
    else:
        create_backup()
    if not confirm(f"Restore from {backup_path}?", default=False):
        info("Restore cancelled.")
        return

    with tarfile.open(backup_path, "r:gz") as tar:
        safe_extract_tar(tar, APP_ROOT)
    info("Restore complete.")
    warn("If containers were running, review settings and restart watchers as needed.")


def backup_restore_menu() -> None:
    while True:
        banner("Backup / export / restore")
        print(wrap("""
            1) Create backup/export
            2) Restore/import backup
            3) List backups
            0) Back
        """))
        choice = prompt("Choose")
        if choice == "0":
            return
        if choice == "1":
            create_backup()
            pause()
        elif choice == "2":
            restore_backup()
            pause()
        elif choice == "3":
            for path in sorted(BACKUP_DIR.glob("*.tar.gz"), reverse=True):
                print(path)
            pause()
        else:
            warn("Unknown choice.")


# =============================================================================
# systemd wrapper generation
# =============================================================================

def systemd_unit_text(entry: WatchEntry) -> str:
    return textwrap.dedent(f"""
    [Unit]
    Description=Rclone Watcher - {entry.name}
    Requires=docker.service
    After=docker.service network-online.target
    Wants=network-online.target

    [Service]
    Type=oneshot
    WorkingDirectory={entry.stack_dir}
    ExecStart=/usr/bin/docker compose -f {entry.compose_file} up -d
    ExecStop=/usr/bin/docker compose -f {entry.compose_file} stop
    RemainAfterExit=yes
    TimeoutStartSec=0

    [Install]
    WantedBy=multi-user.target
    """).strip() + "\n"


def generate_systemd_units(entries: List[WatchEntry]) -> None:
    banner("Generate systemd wrapper units")
    if not entries:
        warn("No watchers configured.")
        return
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        unit_path = SYSTEMD_DIR / f"{entry.container_name}.service"
        unit_path.write_text(systemd_unit_text(entry), encoding="utf-8")
        info(f"Generated: {unit_path}")
    print(wrap(f"""
        To install system-wide units manually:
          sudo cp {SYSTEMD_DIR}/*.service /etc/systemd/system/
          sudo systemctl daemon-reload
          sudo systemctl enable --now rclone-watch-<name>.service

        These units call Docker Compose. They do not replace Docker's own
        restart policy; they simply make host boot behavior more explicit.
    """))


def systemd_menu(entries: List[WatchEntry]) -> None:
    while True:
        banner("systemd wrapper")
        print(wrap("""
            1) Generate systemd unit files for all watchers
            2) Show install instructions
            0) Back
        """))
        choice = prompt("Choose")
        if choice == "0":
            return
        if choice == "1":
            generate_systemd_units(entries)
            pause()
        elif choice == "2":
            print(wrap(f"""
                Generated units live here:
                  {SYSTEMD_DIR}

                Install:
                  sudo cp {SYSTEMD_DIR}/*.service /etc/systemd/system/
                  sudo systemctl daemon-reload

                Enable one:
                  sudo systemctl enable --now rclone-watch-documents.service

                Disable one:
                  sudo systemctl disable --now rclone-watch-documents.service
            """))
            pause()
        else:
            warn("Unknown choice.")


# =============================================================================
# Startup validation and first-run wizard
# =============================================================================

def startup_validation(settings: Settings) -> List[str]:
    findings = []
    system, machine = get_platform_summary()
    findings.append(f"OS: {system} ({machine})")
    if system not in SUPPORTED_RUNTIME_OS:
        findings.append("WARNING: watcher runtime is most reliable on Linux due to inotify")
    findings.append(f"Docker installed: {'yes' if docker_installed() else 'no'}")
    findings.append(f"Docker reachable: {'yes' if docker_ready() else 'no'}")
    findings.append(f"Docker Compose: {'yes' if docker_compose_available() else 'no'}")
    findings.append(f"rclone installed: {'yes' if rclone_installed() else 'no'}")
    findings.append(f"rclone configured: {'yes' if rclone_configured() else 'no'}")
    findings.append(f"shared image: {'yes' if shared_image_exists() else 'no'}")
    return findings


def show_startup_validation(settings: Settings) -> None:
    banner("Startup validation")
    for line in startup_validation(settings):
        print(f"- {line}")


def run_first_run_wizard(settings: Settings) -> Settings:
    banner(f"{APP_DISPLAY_NAME} - First Run Setup Wizard")
    print(wrap("""
        This wizard checks dependencies, sets up rclone, and builds the shared
        watcher image. You can skip steps and come back later.
    """))
    show_startup_validation(settings)
    if not docker_installed() and confirm("Install Docker now?", default=True):
        install_docker()
    if not rclone_installed() and confirm("Install rclone now?", default=True):
        install_rclone()
    if rclone_installed() and not rclone_configured() and confirm("Run rclone config now?", default=True):
        setup_rclone_configuration(settings)
    if docker_ready() and docker_compose_available() and confirm("Build shared watcher image now?", default=True):
        build_base_image_if_needed(force=True)
    settings.first_run_complete = True
    save_settings(settings)
    info("First-run wizard complete.")
    return settings


# =============================================================================
# Main menu
# =============================================================================

def main_menu() -> None:
    ensure_base_layout()
    settings = load_settings()
    if settings.color_enabled is False:
        os.environ["NO_COLOR"] = "1"
    if not settings.first_run_complete:
        settings = run_first_run_wizard(settings)
        pause()

    while True:
        settings = load_settings()
        entries = load_registry()
        banner(f"{APP_DISPLAY_NAME} v{APP_VERSION}")
        print(wrap("""
             1) Health dashboard
             2) Install Docker
             3) Install rclone
             4) Run rclone configuration setup
             5) Remote test / check menu
             6) Folder list / statuses / start / stop / logs
             7) Add a folder to watch
             8) Edit a folder entry
             9) Remove a folder from the list
            10) Build or rebuild shared watcher image
            11) Settings editor
            12) Backup / export / restore
            13) systemd wrapper generator
            14) Startup validation
            15) Re-run first-run setup wizard
             0) Exit
        """))
        choice = prompt("Choose an option")

        if choice == "0":
            print("Goodbye.")
            return
        if choice == "1":
            show_dashboard(entries, settings)
            pause()
        elif choice == "2":
            install_docker()
            pause()
        elif choice == "3":
            install_rclone()
            pause()
        elif choice == "4":
            setup_rclone_configuration(settings)
            pause()
        elif choice == "5":
            remote_check_menu(settings)
            pause()
        elif choice == "6":
            status_control_menu(entries, settings)
            pause()
        elif choice == "7":
            entries = add_watch_folder(entries, settings)
            save_registry(entries)
            pause()
        elif choice == "8":
            entries = edit_watch_folder(entries, settings)
            save_registry(entries)
            pause()
        elif choice == "9":
            entries = remove_watch_folder(entries)
            save_registry(entries)
            pause()
        elif choice == "10":
            build_base_image_if_needed(force=True)
            pause()
        elif choice == "11":
            settings = settings_editor(settings)
            save_settings(settings)
            pause()
        elif choice == "12":
            backup_restore_menu()
            pause()
        elif choice == "13":
            systemd_menu(entries)
            pause()
        elif choice == "14":
            show_startup_validation(settings)
            pause()
        elif choice == "15":
            settings = run_first_run_wizard(settings)
            save_settings(settings)
            pause()
        else:
            warn("Unknown option.")
            pause()


# =============================================================================
# Entrypoint
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_DISPLAY_NAME} - manage Docker-powered rclone folder watchers.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the application version and exit.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run startup validation and exit without opening the menu.",
    )
    args = parser.parse_args()

    if args.version:
        print(f"{APP_DISPLAY_NAME} v{APP_VERSION}")
        return 0

    try:
        ensure_base_layout()
        settings = load_settings()

        if args.validate:
            show_startup_validation(settings)
            return 0

        main_menu()
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
