# Server Health & Security Monitor

> คู่มือติดตั้งและใช้งานแบบละเอียดอยู่ใน [`INSTALL.md`](INSTALL.md) — รวมขั้นตอนติดตั้ง การเชื่อมต่อ API/dashboard/collector ข้อมูลที่เริ่มใหม่บนเครื่องใหม่ การสำรองข้อมูล และการถอนการติดตั้ง

## Project Structure

```text
server-health-monitor/
|-- backend/
|   |-- .env.example
|   |-- requirements.txt
|   `-- app/
|       |-- __init__.py
|       |-- api.py
|       |-- config.py
|       |-- database.py
|       |-- main.py
|       |-- models.py
|       |-- schemas.py
|       `-- security.py
|-- collector/
|   |-- collector.py
|   `-- security_checker.py
|-- frontend/
|   `-- index.html
|-- .gitignore
`-- README.md
```

## Quick start (Windows)

Prereqs: Python 3.11+, MongoDB running on `127.0.0.1:27017` (native service) or Docker Desktop.

```powershell
# Elevated PowerShell (registers autostart + watchdog tasks)
.\Install.cmd
# or: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`install.ps1` is idempotent: creates `collector\.venv`, installs deps, creates `backend\.env` with fresh random secrets if missing, registers 5 tasks (API/Dashboard/Collector/Watchdog/Backup), waits for `/health`+dashboard, asks for the first admin only when none exists, then opens the dashboard. `-NoAutostart` installs without tasks; `-Reinstall` reinstalls Python deps. The installer finds a usable Python on its own (the `python` command on PATH is often the Microsoft Store stub, which cannot create a virtual environment).

Tasks: the API and Dashboard run as the logged-on user, the Collector and the daily backup run as the local system account. The Collector needs the Security event log, and an interactive "highest" task only receives the admin token when the session that starts it is elevated, so the system account is the reliable choice.

MongoDB: the installer uses the local `MongoDB` service when it can reach `127.0.0.1:27017`, otherwise it starts MongoDB with Docker Compose. Notes for the Docker path:

- `docker-compose.yml` keeps the MongoDB data in a Docker named volume. WiredTiger hangs on Docker Desktop bind mounts, so Windows must use the named volume; on Linux use `docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d` to keep the data files in `./data/mongo` inside the project folder.
- MongoDB 5.0+ needs a CPU with AVX support. On older CPUs the container crashes; the installer detects the `AVX` warning in the container log and falls back to `mongo:4.4` (override it yourself with `MONGO_IMAGE`).
- The container only runs while Docker Desktop runs, so Docker Desktop has to start at logon (Settings → General) if MongoDB lives in a container.

Backups: `scripts\backup.ps1` copies the configuration database, `.env` and the collector state into `backups\<timestamp>` and keeps 14 days. The `Server Health Monitor Backup` task runs it daily at 03:00.

## Desktop app

`Sentinel Desktop.cmd` (or the `Sentinel` shortcut the installer puts on the desktop) opens the dashboard in a native window instead of a browser. It is a PyWebView shell around the same local dashboard, so the UI never drifts from the web version. The shell starts the API and dashboard tasks if they are not running yet, waits for them, and then shows the window; when the services stay unreachable it explains that instead of opening a broken page. The desktop build keeps the access token in local storage so a restart stays signed in, while the browser build keeps it in the tab session so it never reaches the disk. `desktop\requirements.txt` holds `pywebview`, which needs the Edge WebView2 runtime that ships with Windows 10 and 11; the installer treats it as optional.

Python dependencies: `uvicorn` is installed without its `[standard]` extra and `pip-audit` lives in `collector\requirements-audit.txt`, because both pull in packages that have no wheel for the newest Python versions and would need a C compiler. The installer treats the audit tool as optional; the package audit then runs without the vulnerability scan.

Steps 2, 3, and 4 are implemented under `backend/`, `collector/`, and `frontend/` respectively.

## Manual setup (advanced)

The API uses SQLite by default for users and monitoring configuration, and MongoDB for metric samples, security events, and scan results. MongoDB being unavailable does not prevent FastAPI from starting; Mongo-backed requests return HTTP 503 and `/health` reports its status. Each collection has a TTL index so MongoDB deletes old documents by itself: `METRICS_RETENTION_DAYS` (default 7), `SECURITY_EVENTS_RETENTION_DAYS` (default 30) and `SCAN_RESULTS_RETENTION_DAYS` (default 30); set a value to `0` to keep that collection forever. The Settings panel has a Data retention form backed by `GET`/`PUT /api/v1/retention`; saving rebuilds the TTL index immediately, while the `.env` values only seed the defaults on first run.

From the repository root, copy `backend/.env.example` to `backend/.env` and replace all three secrets with different random values of at least 32 characters. Keep `.env` private. Install and launch:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The interactive API reference is at `http://127.0.0.1:8000/docs`. MongoDB must be running for collector ingestion and dashboard metrics. Use `/health` to see whether it is reachable.

## Windows Autostart

`Install.cmd` / `scripts/install.ps1` register the 4 logon tasks (API, Dashboard, Collector, Watchdog) and start them. To manage tasks manually from an elevated PowerShell at the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_windows_autostart.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_watchdog.ps1
```

The API and Dashboard tasks run as the current user with normal privileges. The Collector task runs with the current user's highest available privileges so it can read the Security event log. No Windows account password is stored by these tasks. They start at the next user logon; existing manually started processes are not stopped. The API and Dashboard bind to localhost only. Remove tasks with `scripts\uninstall_windows_autostart.ps1` from an elevated PowerShell (`-RemoveData` also removes `data/`, `backend\monitor.db`, logs, and `collector\state`).

## Steps 3 and 4: Collector and Dashboard

Install the collector dependencies with `pip install -r collector/requirements.txt`. Set `COLLECTOR_API_KEY` to the same secret used by the API, then run `python collector.py` from the `collector/` directory. Configure `MONITOR_HOST_ID` to name this host in the dashboard, and `MONITOR_KNOWN_PORTS` as a comma-separated local port allowlist. By default, file integrity checks hash `backend/.env`, the backend config/security/API/startup modules, and both collector modules. Set `MONITOR_CRITICAL_FILES` to a platform-separated list of local paths to override these defaults. Package auditing runs in a background thread every `MONITOR_PACKAGE_CHECK_HOURS` hours (default 6) so the 15-second metric sampling is never blocked; it lists outdated Python packages, audits them for known vulnerabilities when `pip-audit` is installed (`pip install pip-audit` into the collector environment), and checks Windows Update (or `apt list --upgradable` on Linux). The Windows Update search runs online first and, if that takes longer than 4 minutes, falls back to the local update cache and labels those findings as coming from the cached list.

On Linux, `MONITOR_AUTH_LOGS` can specify comma-separated readable authentication log paths; the defaults are `/var/log/auth.log` and `/var/log/secure`. On Windows, the collector reads failed-login Event ID 4625 from the local Security log, checks pending software updates with Windows Update Agent, and reports the ACPI thermal-zone temperature when firmware exposes it. Failed-logon detection is incremental: each cycle asks only for records newer than the stored `last_record_id`, oldest first, in pages of 200 (at most 5 pages per cycle), so it no longer re-reads the newest 1000 events and time out. A cleared Security log is detected and rebaselined instead of stalling the check. That thermal-zone value is not labeled as CPU-die temperature. Run the collector from an elevated PowerShell session to read the Security log and Windows thermal zone. If a sensor is unavailable, the dashboard displays that state rather than estimating a value. The collector reads only local machine data; it does not connect to arbitrary hosts. The file hash baseline and log offsets are stored in `collector/state/` and should be protected from modification.

Collector uploads are retried instead of dropped: when the API is unreachable (connection refused, timeout, 5xx) the payload is appended to `collector/state/spool.jsonl` and every later cycle replays the queue oldest-first before collecting new data, printing `Replayed N spooled payload(s)`. Payloads the API rejects with a 4xx status are dropped, since retrying them would never succeed. The queue is capped at `MONITOR_SPOOL_MAX_ITEMS` entries (default 5000, oldest dropped first), replays at most `MONITOR_SPOOL_FLUSH_LIMIT` payloads (default 200) within `MONITOR_SPOOL_FLUSH_SECONDS` (default 5) per cycle, and survives collector restarts.

Repeating findings are not reported again on every cycle: an unapproved listening port and a changed critical file each produce one security event per cooldown window (`MONITOR_PORT_EVENT_COOLDOWN_SECONDS` and `MONITOR_FILE_EVENT_COOLDOWN_SECONDS`, both 3600 seconds). The scan results still carry the full current picture, so the dashboard stays accurate while the event store no longer fills up with duplicates.

Serve the dashboard from the repository root with `python -m http.server 8001 --directory frontend` and open `http://127.0.0.1:8001`. The matching local CORS origins are the defaults. The browser stores the bearer token only in the current tab's `sessionStorage`; it never receives the collector key.

Create the initial account once with `POST /api/v1/auth/register`, passing `X-Bootstrap-Token` from `.env` and a password of at least 12 characters. Registration is disabled until a bootstrap token is configured and returns conflict after the initial account exists. Then call `POST /api/v1/auth/login` with JSON username/password and use the returned bearer token for dashboard and configuration APIs.

Collector endpoints require `X-Collector-Key`: `POST /api/v1/collector/metrics`, `POST /api/v1/collector/security-events`, and `POST /api/v1/collector/scan-results`. Every collector payload carries a host identifier: the collector sends `MONITOR_HOST_ID` when it is set and otherwise the machine hostname. Samples stored before this field existed are reported as `unassigned`, so the dashboard stays usable with existing data.

Authenticated dashboard endpoints are `GET /api/v1/dashboard/hosts`, `GET /api/v1/dashboard/latest?hours=24&host=web-01`, and `GET /api/v1/dashboard/history?hours=24&host=web-01`; `hours` accepts 1-720 and scopes the security events and scan results returned by `latest`, as well as the metric history. The dashboard header provides a host selector (fed by `hosts` in the `latest` response), a 30m/1h/6h/24h/7d range selector, and the Settings panel edits system thresholds, custom alert rules, the IP allowlist, and alert notification channels. The thresholds form has a scope selector (global or a single host) plus a remove-override action, and the rule form picks the host a rule applies to. Omitting `host` evaluates every host seen in the window and aggregates their alerts; the `/alerts` endpoint accepts the same `host` filter. Thresholds and alert rules are scoped per host: `GET /api/v1/thresholds?host=<name>` returns the effective values for that host (falling back to the global row), `PUT` with the same query stores an override, `DELETE` removes it, and `GET /api/v1/thresholds/overrides` lists every host that has one. Alert rules carry an optional `host` (empty means every host) and are only evaluated against their own host, using that host's threshold values.

`GET /api/v1/dashboard/latest` returns scan results balanced across scan types, always includes recent `failed_login` events even when high-volume listening-port events fill the window, and always includes the newest scan of each type per host even when it falls outside the selected range (package audits run every few hours, so a one-hour view would otherwise hide them). Persisted alerts are available from `GET /api/v1/alerts` (optional `status` filter of `new`, `acknowledged`, or `resolved`, plus `limit`) and their state is changed with `PUT /api/v1/alerts/{id}` accepting `{"status": "new" | "acknowledged" | "resolved"}`. Alerts are recorded with a stable fingerprint after each ingested metric sample, so the same condition updates `last_seen` instead of creating duplicates; a resolved alert that reappears is reopened as `new`. Notifications are only sent for newly created or reopened alerts.

The dashboard updates itself from `GET /api/v1/dashboard/stream`, a server-sent-event feed authenticated with the normal bearer token: every collector ingest publishes `metrics`, `security_events` or `scan_results`, throttled to one event per kind per second. Metric cards and the "Updated …" line are painted straight from each `metrics` event, while the heavier tables are refetched at most every 10 seconds. Granularity is the collector interval (`MONITOR_POLL_SECONDS`, default 15). The feed is in-process, so it needs a single API worker.

The collector writes `collector/state/collector.log` as UTF-8 (an older UTF-16 log is converted on the next start), so the file opens in any editor and greps cleanly.

Alert notifications use `/api/v1/notification-channels`: `GET` and `POST` on the collection, `PUT` and `DELETE` on `/api/v1/notification-channels/{id}`, and `POST /api/v1/notification-channels/{id}/test` for a test delivery. Slack and Discord receive a `text`/`content` message, Telegram posts to `https://api.telegram.org/bot<token>/sendMessage` with the stored chat ID (the `webhook_url` field accepts the bare BotFather token as well as the full URL, and is expanded to the correct endpoint), and a custom webhook receives the full structured alert payload. Alert checks run after each ingested metric sample on a background thread, identical payloads are not sent twice, and webhook URLs are stored server-side and only returned masked. The authenticated SQL-backed APIs provide whitelist list/add/delete, alert-rule list/create/update/delete, and threshold get/update under `/api/v1/whitelist`, `/api/v1/alert-rules`, and `/api/v1/thresholds`.

## Tests and Lint

Run the checks from the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_checks.ps1
```

`Run Checks.cmd` runs the same script from Explorer. It uses `collector/.venv/Scripts/python.exe` when that environment exists (falling back to `python` on PATH), byte-compiles the backend packages and both collector modules, runs the backend and collector unit tests with `unittest discover`, and lints with `ruff` using `ruff.toml`. Install the lint tool into the project environment once with `pip install -r backend/requirements-dev.txt`; without ruff the lint step is skipped with an install hint.

## Watchdog

`scripts\watchdog.ps1` performs one check pass: it calls the API health endpoint, requests the dashboard, and reads the newest metric sample to detect a stalled collector. When a check fails it restarts the matching scheduled task and writes a timestamped line to `logs\watchdog.log`. A service that is still starting gets a grace period of 60 seconds before it counts as down, and a stopped MongoDB service is started again before the API is touched, because restarting the API cannot fix the database.

Register the repeating task once from an elevated PowerShell prompt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_watchdog.ps1
```

After each restart attempt the watchdog posts a message through every enabled notification channel (Settings → Alert notifications) by running `scripts\watchdog_notify.py`, which reuses the same channel formatting as regular alerts: success and failure are both reported, and when no channel is enabled the run logs `skip:no-enabled-channels`.

This creates `Server Health Monitor Watchdog`, which runs at logon and then every 5 minutes with highest privileges (needed to restart the collector task and to read its process information). Change the cadence with `-IntervalMinutes <n>`. Run `scripts\watchdog.ps1` directly for a single check; `-CollectorMaxAgeMinutes` controls how old the newest sample may be before the collector counts as stale. The watchdog log is rotated once it passes 10 MB.

## Security Notes

- Bind the API to a trusted interface, use HTTPS behind a production reverse proxy, and restrict CORS to the actual dashboard origin.
- Put `.env`, the SQLite database, collector state, and logs outside source control; never expose the collector key in the browser.
- `scripts\backup.ps1` keeps a copy of the configuration database, `.env` and the collector state under `backups\` and runs daily through the `Server Health Monitor Backup` task; the monitor time series live in MongoDB and are not part of that backup, so export them separately if they matter.
- Sign-in is throttled per client address (`LOGIN_WINDOW_SECONDS`, default 300, with `LOGIN_MAX_PER_WINDOW`, default 20 attempts) and the account locks after `LOGIN_MAX_ATTEMPTS` failures (default 5) for `LOGIN_LOCKOUT_MINUTES` (default 15); both answer `429` with `Retry-After`, and a successful sign-in clears the counter. Add operational backups and, for multiple operators, a real account model before production: this starter intentionally has a single initial administrator account, not public multi-user registration.
- Port inventory should run on the monitored host and only enumerate local listening sockets. Do not expose it as a remote arbitrary-target scanner.