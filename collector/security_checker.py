import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

FAILED_PASSWORD = re.compile(
    r"Failed (?:password|publickey) for (?:invalid user )?(?P<username>\S+) from (?P<ip>[0-9a-fA-F:.]+)"
)
AUTH_FAILURE = re.compile(
    r"authentication failure;.*?rhost=(?P<ip>[^\s]+).*?(?:ruser=(?P<username>[^\s]*))?"
)
ISO_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)")
SYSLOG_TIMESTAMP = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})")


def log_timestamp(line: str) -> str:
    iso_match = ISO_TIMESTAMP.match(line)
    if iso_match:
        try:
            parsed = datetime.fromisoformat(iso_match.group(1).replace("Z", "+00:00"))
            return parsed.astimezone().isoformat()
        except ValueError:
            pass

    syslog_match = SYSLOG_TIMESTAMP.match(line)
    if syslog_match:
        try:
            now = datetime.now().astimezone()
            parsed = datetime.strptime(syslog_match.group(1), "%b %d %H:%M:%S").replace(year=now.year)
            parsed = parsed.astimezone()
            if parsed > now.replace(microsecond=0):
                parsed = parsed.replace(year=parsed.year - 1)
            return parsed.isoformat()
        except ValueError:
            pass

    return datetime.now().astimezone().isoformat()


def listening_ports(known_ports: set[int]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError, NotImplementedError) as exc:
        return [{"status": "unavailable", "reason": type(exc).__name__}]

    seen: set[tuple[str, int, int | None]] = set()
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        address = connection.laddr.ip
        port = connection.laddr.port
        key = (address, port, connection.pid)
        if key in seen:
            continue
        seen.add(key)
        try:
            process = psutil.Process(connection.pid).name() if connection.pid else "unknown"
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            process = "unknown"
        findings.append(
            {
                "address": address,
                "port": port,
                "process": process,
                "pid": connection.pid,
                "known": port in known_ports,
            }
        )
    return findings


def failed_login_events(log_paths: list[Path], offset_file: Path) -> list[dict[str, Any]]:
    if os.name == "nt":
        return windows_failed_login_events(offset_file)

    try:
        saved_offsets = json.loads(offset_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        saved_offsets = {}
    if not isinstance(saved_offsets, dict):
        saved_offsets = {}

    new_offsets: dict[str, dict[str, int | str]] = {}
    events: list[dict[str, Any]] = []
    readable_logs = 0
    for path in log_paths:
        try:
            file_stat = path.stat()
            identity = f"{file_stat.st_dev}:{file_stat.st_ino}"
            previous = saved_offsets.get(str(path), {})
            offset = previous.get("offset", 0) if previous.get("identity") == identity else 0
            if not isinstance(offset, int) or offset < 0 or offset > file_stat.st_size:
                offset = 0
            with path.open("rb") as log_file:
                log_file.seek(offset)
                content = log_file.read()
                new_offsets[str(path)] = {"identity": identity, "offset": log_file.tell()}
        except OSError:
            continue
        readable_logs += 1

        for raw_line in content.decode("utf-8", errors="replace").splitlines():
            match = FAILED_PASSWORD.search(raw_line) or AUTH_FAILURE.search(raw_line)
            if not match:
                continue
            source_ip = match.group("ip")
            try:
                source_ip = str(ipaddress.ip_address(source_ip))
            except ValueError:
                continue
            events.append(
                {
                    "timestamp": log_timestamp(raw_line),
                    "category": "failed_login",
                    "severity": "medium",
                    "source_ip": source_ip,
                    "message": "Failed authentication attempt in system log",
                    "details": {"username": match.groupdict().get("username") or "unknown", "log": str(path)},
                }
            )

    if not readable_logs:
        raise OSError("No configured authentication logs could be read")

    offset_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = offset_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(new_offsets), encoding="utf-8")
    os.replace(temporary, offset_file)
    return events


def powershell_output(script: str, timeout: int = 30, extra_env: dict[str, str] | None = None) -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        raise OSError("PowerShell is unavailable")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, **(extra_env or {})},
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "PowerShell command failed")
    return result.stdout.strip()


def system_temperature_celsius() -> float | None:
    if os.name != "nt":
        return None

    script = r"""
$ErrorActionPreference = 'Stop'
try {
    $readings = @(Get-CimInstance -Namespace 'root/wmi' -ClassName 'MSAcpi_ThermalZoneTemperature' -ErrorAction Stop | ForEach-Object {
        [double](($_.CurrentTemperature / 10) - 273.15)
    } | Where-Object { $_ -ge 0 -and $_ -le 150 })
    ConvertTo-Json -InputObject $readings -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""
    try:
        output = powershell_output(script, timeout=10)
        readings = json.loads(output) if output else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    if not isinstance(readings, list):
        readings = [readings]
    valid_readings = [
        float(reading)
        for reading in readings
        if isinstance(reading, (int, float))
        and not isinstance(reading, bool)
        and math.isfinite(reading)
        and 0 <= reading <= 150
    ]
    return max(valid_readings) if valid_readings else None


WINDOWS_LOGIN_PAGE_SIZE = 200
WINDOWS_LOGIN_MAX_PAGES = 5
WINDOWS_LOGIN_TIMEOUT = 45
WINDOWS_LOGIN_REBASE_SECONDS = 600

# Only events newer than the stored RecordId are requested, oldest first, one page at a time.
WINDOWS_LOGIN_PAGE_SCRIPT = r"""
$lastId = [long]$env:MONITOR_LAST_RECORD_ID
$pageSize = [int]$env:MONITOR_PAGE_SIZE
$ErrorActionPreference = 'Stop'
try {
    $rawEvents = @(Get-WinEvent -LogName 'Security' -FilterXPath "*[System[EventID=4625 and EventRecordID > $lastId]]" -MaxEvents $pageSize -Oldest -ErrorAction Stop)
} catch {
    if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') {
        $rawEvents = @()
    } else {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
$records = @($rawEvents | ForEach-Object {
    $eventXml = [xml]$_.ToXml()
    $fields = @{}
    foreach ($entry in $eventXml.Event.EventData.Data) {
        $fields[[string]$entry.Name] = [string]$entry.InnerText
    }
    [pscustomobject]@{
        record_id = [long]$_.RecordId
        timestamp = $_.TimeCreated.ToUniversalTime().ToString('o')
        source_ip = $fields['IpAddress']
        username = $fields['TargetUserName']
    }
})
ConvertTo-Json -InputObject $records -Compress -Depth 4
"""

WINDOWS_LOGIN_LATEST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $events = @(Get-WinEvent -LogName 'Security' -FilterXPath "*[System[EventID=4625]]" -MaxEvents 1 -ErrorAction Stop)
} catch {
    if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') {
        $events = @()
    } else {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
if ($events.Count -gt 0) { [string]$events[0].RecordId } else { '0' }
"""


def windows_state_read(state_file: Path) -> dict[str, Any]:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def windows_state_write(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, state_file)


def windows_login_page(after_record_id: int) -> list[dict[str, Any]]:
    try:
        output = powershell_output(
            WINDOWS_LOGIN_PAGE_SCRIPT,
            timeout=WINDOWS_LOGIN_TIMEOUT,
            extra_env={
                "MONITOR_LAST_RECORD_ID": str(after_record_id),
                "MONITOR_PAGE_SIZE": str(WINDOWS_LOGIN_PAGE_SIZE),
            },
        )
        raw_events = json.loads(output) if output else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise OSError(f"Windows Security event query failed: {short_reason(exc)}") from None
    if isinstance(raw_events, dict):
        raw_events = [raw_events]
    if not isinstance(raw_events, list):
        raise OSError("Windows Security event query returned invalid data")
    return [event for event in raw_events if isinstance(event, dict) and isinstance(event.get("record_id"), int)]


def windows_login_latest_record_id() -> int:
    try:
        output = powershell_output(WINDOWS_LOGIN_LATEST_SCRIPT, timeout=WINDOWS_LOGIN_TIMEOUT)
        return int(str(output).strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise OSError(f"Windows Security event query failed: {short_reason(exc)}") from None


def windows_failed_login_events(state_file: Path) -> list[dict[str, Any]]:
    """Reads only events newer than the stored RecordId in bounded pages.

    The previous query fetched the newest 1000 events on every cycle, which took
    longer than the 30 second timeout and made the whole authentication check fail.
    """
    state = windows_state_read(state_file)
    try:
        previous_record_id = int(state["last_record_id"])
    except (KeyError, TypeError, ValueError):
        previous_record_id = None
    try:
        last_checked = int(state.get("checked_at") or 0)
    except (TypeError, ValueError):
        last_checked = 0

    if previous_record_id is None:
        # Nothing stored yet: baseline on the newest event instead of replaying the log.
        windows_state_write(state_file, {"last_record_id": windows_login_latest_record_id(), "checked_at": int(time.time())})
        return []

    cursor = previous_record_id
    raw_events: list[dict[str, Any]] = []
    for _ in range(WINDOWS_LOGIN_MAX_PAGES):
        page = windows_login_page(cursor)
        if not page:
            break
        raw_events.extend(page)
        cursor = max(event["record_id"] for event in page)
        if len(page) < WINDOWS_LOGIN_PAGE_SIZE:
            break

    events: list[dict[str, Any]] = []
    for event in raw_events:
        if event["record_id"] <= previous_record_id:
            continue
        source_ip = event.get("source_ip")
        try:
            source_ip = str(ipaddress.ip_address(source_ip))
        except (ValueError, TypeError):
            source_ip = None
        events.append(
            {
                "timestamp": event.get("timestamp") or datetime.now().astimezone().isoformat(),
                "category": "failed_login",
                "severity": "medium",
                "source_ip": source_ip,
                "message": "Failed authentication attempt in Windows Security log",
                "details": {"username": event.get("username") or "unknown", "event_id": 4625},
            }
        )

    checked_at = int(time.time())
    if not raw_events and checked_at - last_checked >= WINDOWS_LOGIN_REBASE_SECONDS:
        latest = windows_login_latest_record_id()
        if latest and latest < previous_record_id:
            # The Security log was cleared: continue from the current record range.
            windows_state_write(state_file, {"last_record_id": latest, "checked_at": checked_at})
            return []

    windows_state_write(state_file, {"last_record_id": max(previous_record_id, cursor), "checked_at": checked_at})
    return events


def check_file_integrity(paths: list[Path], baseline_file: Path) -> list[dict[str, Any]]:
    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        baseline = {}
    if not isinstance(baseline, dict):
        baseline = {}

    # Keep only the monitored paths; entries for files that moved away are dead weight.
    monitored_keys = {str(path.resolve(strict=False)) for path in paths}
    baseline = {key: value for key, value in baseline.items() if key in monitored_keys}

    current: dict[str, str] = {}
    findings: list[dict[str, Any]] = []
    for path in paths:
        key = str(path.resolve(strict=False))
        try:
            resolved_path = path.resolve(strict=True)
            if not resolved_path.is_file():
                continue
            digest = hashlib.sha256()
            with resolved_path.open("rb") as file_handle:
                for block in iter(lambda: file_handle.read(1024 * 1024), b""):
                    digest.update(block)
            key = str(resolved_path)
            current[key] = digest.hexdigest()
            prior_digest = baseline.get(key)
            if prior_digest is None:
                baseline[key] = current[key]
                findings.append({"path": key, "status": "baseline_created", "sha256": current[key]})
            elif prior_digest != current[key]:
                findings.append({"path": key, "status": "modified", "sha256": current[key]})
        except FileNotFoundError:
            if key in baseline:
                findings.append({"path": key, "status": "missing"})
        except OSError as exc:
            findings.append({"path": str(path), "status": "unavailable", "reason": type(exc).__name__})

    if current and baseline:
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = baseline_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        os.replace(temporary, baseline_file)
    return findings


PIP_TIMEOUT = 180
PIP_AUDIT_TIMEOUT = 240
WINDOWS_UPDATE_TIMEOUT = 240
WINDOWS_UPDATE_CACHED_TIMEOUT = 90


def short_reason(exc: BaseException, limit: int = 160) -> str:
    """subprocess errors stringify the whole command line; keep only the useful part."""
    if isinstance(exc, subprocess.TimeoutExpired):
        text = f"timed out after {int(exc.timeout or 0)}s"
    else:
        text = str(exc) or type(exc).__name__
    return " ".join(text.split())[:limit]


def windows_update_findings(script: str) -> list[dict[str, Any]]:
    """An online Windows Update search can take minutes, so retry from the local cache."""
    errors: list[str] = []
    attempts = (("1", WINDOWS_UPDATE_TIMEOUT), ("0", WINDOWS_UPDATE_CACHED_TIMEOUT))
    for online, timeout in attempts:
        label = "online" if online == "1" else "cached"
        try:
            output = powershell_output(script, timeout=timeout, extra_env={"MONITOR_WU_ONLINE": online})
            updates = json.loads(output) if output else []
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: {short_reason(exc)}")
            continue
        if isinstance(updates, dict):
            updates = [updates]
        if not isinstance(updates, list):
            errors.append(f"{label}: unexpected data")
            continue
        return [
            {
                "kind": "upgradable_os_package",
                "name": update.get("title") or "Windows Update",
                "kb_articles": update.get("kb_articles", []),
                "source": "windows_update" if online == "1" else "windows_update_cached",
            }
            for update in updates
            if isinstance(update, dict)
        ]
    return [{"kind": "os_package_check_error", "reason": ("windows update search failed (" + "; ".join(errors) + ")")[:240]}]


def package_findings() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        outdated = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--outdated", "--format=json", "--disable-pip-version-check"],
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        outdated = None
        pip_error = short_reason(exc)
    else:
        pip_error = ""
    if outdated is not None and outdated.returncode != 0:
        pip_error = (outdated.stderr or outdated.stdout or "").strip().splitlines()[-1][:160] if (outdated.stderr or outdated.stdout).strip() else f"exit {outdated.returncode}"
    if outdated is not None and outdated.returncode == 0:
        try:
            for package in json.loads(outdated.stdout):
                findings.append(
                    {
                        "kind": "outdated_python_package",
                        "name": package.get("name"),
                        "installed": package.get("version"),
                        "available": package.get("latest_version"),
                    }
                )
        except (json.JSONDecodeError, AttributeError):
            findings.append({"kind": "python_package_check_error", "reason": "invalid pip output"})
    else:
        findings.append({"kind": "python_package_check_error", "reason": f"pip list failed: {pip_error}" if pip_error else "pip list failed"})

    try:
        audit = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--format=json", "--progress-spinner", "off"],
            capture_output=True,
            text=True,
            timeout=PIP_AUDIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        audit = None
        audit_error = short_reason(exc)
    else:
        audit_error = ""
    if audit is not None and audit.returncode not in (0, 1) and audit.stderr.strip():
        audit_error = audit.stderr.strip().splitlines()[-1][:160]
    if audit is not None and audit.returncode in (0, 1):
        try:
            audit_data = json.loads(audit.stdout or "{}")
            for package in audit_data.get("dependencies", []):
                for vulnerability in package.get("vulns", []):
                    findings.append(
                        {
                            "kind": "vulnerable_python_package",
                            "name": package.get("name"),
                            "version": package.get("version"),
                            "id": vulnerability.get("id"),
                            "fix_versions": vulnerability.get("fix_versions", []),
                        }
                    )
        except (json.JSONDecodeError, AttributeError):
            findings.append({"kind": "python_vulnerability_check_error", "reason": "invalid pip-audit output"})
    else:
        findings.append(
            {
                "kind": "python_vulnerability_check_unavailable",
                "reason": f"pip-audit failed: {audit_error}" if audit_error else "install pip-audit to enable vulnerability checks",
            }
        )

    if sys.platform.startswith("linux") and Path("/usr/bin/apt").exists():
        try:
            apt = subprocess.run(
                ["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            apt = None
        if apt is None or apt.returncode != 0:
            return findings + [{"kind": "os_package_check_error", "reason": "apt check failed"}]
        for line in apt.stdout.splitlines()[1:]:
            package_name = line.split("/", 1)[0].strip()
            if package_name:
                findings.append({"kind": "upgradable_os_package", "name": package_name, "source": "apt"})
    elif os.name == "nt":
        script = r"""
$ErrorActionPreference = 'Stop'
try {
    $searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
    $searcher.ClientApplicationID = 'Server Health Monitor'
    if ($env:MONITOR_WU_ONLINE -eq '0') { $searcher.Online = $false }
    $result = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
    $updates = @()
    for ($index = 0; $index -lt $result.Updates.Count; $index++) {
        $update = $result.Updates.Item($index)
        $updates += [pscustomobject]@{ title = $update.Title; kb_articles = @($update.KBArticleIDs) }
    }
    ConvertTo-Json -InputObject $updates -Compress -Depth 4
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""
        findings.extend(windows_update_findings(script))
    return findings