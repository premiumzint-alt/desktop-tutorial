import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil
from security_checker import (
    check_file_integrity,
    failed_login_events,
    listening_ports,
    package_findings,
    system_temperature_celsius,
)

API_URL = os.getenv("MONITOR_API_URL", "http://127.0.0.1:8000").rstrip("/")
COLLECTOR_KEY = os.getenv("COLLECTOR_API_KEY", "")
HOST_ID = (os.getenv("MONITOR_HOST_ID") or socket.gethostname() or "unassigned").strip()[:80]
POLL_SECONDS = max(5, int(os.getenv("MONITOR_POLL_SECONDS", "15")))
PACKAGE_CHECK_HOURS = max(1, int(os.getenv("MONITOR_PACKAGE_CHECK_HOURS", "6")))
KNOWN_PORTS = {
    int(port.strip())
    for port in os.getenv("MONITOR_KNOWN_PORTS", "22,80,443").split(",")
    if port.strip().isdigit() and 0 <= int(port.strip()) <= 65535
}
STATE_DIR = Path(os.getenv("MONITOR_STATE_DIR", "state"))
SPOOL_PATH = STATE_DIR / "spool.jsonl"
PORT_EVENT_STATE_PATH = STATE_DIR / "port_events.json"
FILE_EVENT_STATE_PATH = STATE_DIR / "file_events.json"
PORT_EVENT_COOLDOWN_SECONDS = max(0, int(os.getenv("MONITOR_PORT_EVENT_COOLDOWN_SECONDS", "3600")))
FILE_EVENT_COOLDOWN_SECONDS = max(0, int(os.getenv("MONITOR_FILE_EVENT_COOLDOWN_SECONDS", "3600")))
SPOOL_MAX_ITEMS = max(100, int(os.getenv("MONITOR_SPOOL_MAX_ITEMS", "5000")))
SPOOL_FLUSH_LIMIT = max(1, int(os.getenv("MONITOR_SPOOL_FLUSH_LIMIT", "200")))
SPOOL_FLUSH_SECONDS = max(1, int(os.getenv("MONITOR_SPOOL_FLUSH_SECONDS", "5")))
SPOOL_LOCK = threading.Lock()
LOG_PATHS = [Path(path) for path in os.getenv("MONITOR_AUTH_LOGS", "/var/log/auth.log,/var/log/secure").split(",") if path]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INTEGRITY_PATHS = [
    PROJECT_ROOT / "backend" / ".env",
    PROJECT_ROOT / "backend" / "app" / "config.py",
    PROJECT_ROOT / "backend" / "app" / "security.py",
    PROJECT_ROOT / "backend" / "app" / "api.py",
    PROJECT_ROOT / "backend" / "app" / "main.py",
    PROJECT_ROOT / "collector" / "collector.py",
    PROJECT_ROOT / "collector" / "security_checker.py",
]
configured_integrity_paths = os.getenv("MONITOR_CRITICAL_FILES")
INTEGRITY_PATHS = (
    [Path(path) for path in configured_integrity_paths.split(os.pathsep) if path]
    if configured_integrity_paths
    else DEFAULT_INTEGRITY_PATHS
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_payload(endpoint: str, payload: dict[str, Any]) -> None:
    request = Request(
        f"{API_URL}/api/v1/collector/{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Collector-Key": COLLECTOR_KEY},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        response.read()


def read_spool() -> list[dict[str, Any]]:
    try:
        lines = SPOOL_PATH.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "endpoint" in item and "payload" in item:
            items.append(item)
    return items


def write_spool(items: list[dict[str, Any]]) -> None:
    if not items:
        try:
            SPOOL_PATH.unlink()
        except FileNotFoundError:
            pass
        return
    SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SPOOL_PATH.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    os.replace(temporary, SPOOL_PATH)


def spool_append(endpoint: str, payload: dict[str, Any]) -> None:
    """Keeps the payload on disk so the next cycles can deliver it once the API is back."""
    with SPOOL_LOCK:
        items = read_spool()
        items.append({"endpoint": endpoint, "payload": payload})
        dropped = max(0, len(items) - SPOOL_MAX_ITEMS)
        if dropped:
            items = items[dropped:]
        write_spool(items)
        size = len(items)
    if dropped:
        print(f"Spool full: dropped {dropped} oldest payload(s); {size} queued", flush=True)


def flush_spool() -> None:
    """Replays queued payloads in order; stops at the first failure and keeps the rest."""
    if not SPOOL_PATH.exists():
        return
    sent = 0
    with SPOOL_LOCK:
        items = read_spool()
        deadline = time.monotonic() + SPOOL_FLUSH_SECONDS
        remaining = items[:]
        while remaining and sent < SPOOL_FLUSH_LIMIT and time.monotonic() < deadline:
            item = remaining[0]
            try:
                post_payload(item["endpoint"], item["payload"])
            except HTTPError as exc:
                if 400 <= exc.code < 500:
                    print(f"Dropped spooled {item['endpoint']}: HTTP {exc.code}", flush=True)
                    remaining.pop(0)
                    continue
                break
            except (URLError, TimeoutError, OSError):
                break
            remaining.pop(0)
            sent += 1
        write_spool(remaining)
        queued = len(remaining)
    if sent:
        print(f"Replayed {sent} spooled payload(s); {queued} still queued", flush=True)


def send(endpoint: str, payload: dict[str, Any]) -> None:
    try:
        post_payload(endpoint, payload)
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            # A rejected payload will never be accepted; queuing it would retry forever.
            print(f"Rejected {endpoint}: HTTP {exc.code}", flush=True)
            return
        print(f"Failed to send {endpoint}: {exc}; spooled", flush=True)
        spool_append(endpoint, payload)
    except (URLError, TimeoutError, OSError) as exc:
        print(f"Failed to send {endpoint}: {exc}; spooled", flush=True)
        spool_append(endpoint, payload)


def cpu_temperature() -> float | None:
    try:
        sensors = psutil.sensors_temperatures(fahrenheit=False)
        readings = [entry.current for entries in sensors.values() for entry in entries if entry.current is not None]
        return max(readings) if readings else None
    except (AttributeError, OSError):
        return None


def collect_metrics() -> dict[str, Any]:
    disk_path = os.environ.get("MONITOR_DISK_PATH", Path.cwd().anchor or "/")
    disk = psutil.disk_usage(disk_path)
    network = psutil.net_io_counters()
    return {
        "host": HOST_ID,
        "timestamp": now(),
        "cpu_percent": psutil.cpu_percent(interval=1),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_percent": disk.percent,
        "network_bytes_sent": network.bytes_sent,
        "network_bytes_received": network.bytes_recv,
        "cpu_temp_celsius": cpu_temperature(),
        "system_temp_celsius": system_temperature_celsius(),
    }


def emit_scan(scan_type: str, findings: list[dict[str, Any]]) -> None:
    send(
        "scan-results",
        {"host": HOST_ID, "timestamp": now(), "scan_type": scan_type, "findings": findings},
    )


def read_event_state(path: Path) -> dict[str, float]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {str(key): float(value) for key, value in state.items()} if isinstance(state, dict) else {}


def write_event_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def select_new_findings(
    findings: list[dict[str, Any]],
    state: dict[str, float],
    now_seconds: float,
    cooldown_seconds: int,
    key_of: Any,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Keeps one event per distinct finding per cooldown window.

    Every cycle used to report all findings again, which filled the event store with
    duplicates and pushed the real findings out of the dashboard.
    """
    emitted: list[dict[str, Any]] = []
    updated = dict(state)
    for finding in findings:
        key = key_of(finding)
        last_seen = state.get(key)
        if last_seen is None or now_seconds - last_seen >= cooldown_seconds:
            emitted.append(finding)
            updated[key] = now_seconds
    return emitted, updated


def collect_once() -> None:
    send("metrics", collect_metrics())

    port_findings = listening_ports(KNOWN_PORTS)
    emit_scan("open_ports", port_findings)
    port_state = read_event_state(PORT_EVENT_STATE_PATH)
    new_ports, port_state = select_new_findings(
        port_findings,
        port_state,
        time.time(),
        PORT_EVENT_COOLDOWN_SECONDS,
        lambda finding: str(finding.get("port")),
    )
    write_event_state(PORT_EVENT_STATE_PATH, port_state)
    for finding in new_ports:
        send(
            "security-events",
            {
                "host": HOST_ID,
                "timestamp": now(),
                "category": "unknown_listening_port",
                "severity": "high",
                "message": f"Unapproved local listening port {finding['port']}",
                "details": finding,
            },
        )

    try:
        events = failed_login_events(LOG_PATHS, STATE_DIR / "log_offsets.json")
    except (OSError, ValueError, TimeoutError) as exc:
        reason = str(exc)
        print(f"Failed login check unavailable: {reason}", flush=True)
        emit_scan("failed_logins", [{"status": "unavailable", "reason": reason}])
    else:
        for event in events:
            send("security-events", {**event, "host": HOST_ID})
        emit_scan(
            "failed_logins",
            [
                {
                    "source_ip": event["source_ip"],
                    "username": event["details"]["username"],
                }
                for event in events
            ],
        )

    integrity_findings = check_file_integrity(INTEGRITY_PATHS, STATE_DIR / "file_hashes.json")
    emit_scan("file_integrity", integrity_findings)
    integrity_state = read_event_state(FILE_EVENT_STATE_PATH)
    changed_files, integrity_state = select_new_findings(
        [finding for finding in integrity_findings if finding.get("status") in {"modified", "missing"}],
        integrity_state,
        time.time(),
        FILE_EVENT_COOLDOWN_SECONDS,
        lambda finding: str(finding.get("path")) + ":" + str(finding.get("status")),
    )
    write_event_state(FILE_EVENT_STATE_PATH, integrity_state)
    for finding in changed_files:
        send(
            "security-events",
            {
                "host": HOST_ID,
                "timestamp": now(),
                "category": "modified_file",
                "severity": "high",
                "message": "Critical file is missing or differs from its stored baseline",
                "details": finding,
            },
        )


def package_audit_once(running: threading.Event) -> None:
    """Runs off the sampling loop: pip and Windows Update scans can take minutes."""
    try:
        emit_scan("package_audit", package_findings())
    except Exception as exc:  # never let a background scan kill the collector
        print(f"Package audit failed: {exc}", flush=True)
    finally:
        running.clear()


def main() -> None:
    if len(COLLECTOR_KEY) < 32:
        raise SystemExit("Set COLLECTOR_API_KEY to the same random secret (at least 32 characters) as the API.")
    next_package_check = 0.0
    audit_running = threading.Event()
    while True:
        started = time.monotonic()
        try:
            flush_spool()
            collect_once()
            if started >= next_package_check and not audit_running.is_set():
                next_package_check = started + PACKAGE_CHECK_HOURS * 3600
                audit_running.set()
                threading.Thread(target=package_audit_once, args=(audit_running,), daemon=True).start()
        except (OSError, ValueError, psutil.Error, TimeoutError) as exc:
            print(f"Collection cycle failed: {exc}", flush=True)
        time.sleep(max(1, POLL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()