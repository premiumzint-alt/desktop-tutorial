import asyncio
import hmac
import json
import socket
import time
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pymongo.errors import PyMongoError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db, mongo_database
from .models import (
    Alert,
    AlertRule,
    HostThreshold,
    IPWhitelist,
    NotificationChannel,
    SystemThreshold,
    User,
)
from .notifications import (
    DEFAULT_HOST,
    alert_fingerprint,
    alert_message,
    alert_severity,
    build_alert_payload,
    channel_body,
    send_alert_webhook,
)
from .realtime import hub
from .retention import apply_retention, retention_days, retention_record
from .schemas import (
    AccountUpdateRequest,
    AlertResponse,
    AlertRuleCreate,
    AlertRuleResponse,
    AlertRuleUpdate,
    AlertStatusUpdate,
    EffectiveThresholds,
    HostThresholdsResponse,
    IPWhitelistCreate,
    IPWhitelistResponse,
    LoginRequest,
    MetricsIn,
    NotificationChannelCreate,
    NotificationChannelResponse,
    NotificationChannelUpdate,
    RegisterRequest,
    RetentionSettings,
    ScanResultIn,
    SecurityEventIn,
    ThresholdsUpdate,
    TokenResponse,
    UserResponse,
)
from .security import (
    create_access_token,
    get_current_user,
    hash_password,
    require_collector_key,
    verify_password,
)

router = APIRouter(prefix="/api/v1")


def mongo_collection(name: str):
    try:
        mongo_database.client.admin.command("ping")
        return mongo_database[name]
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Time-series store is unavailable") from None


def as_document(model: Any) -> dict[str, Any]:
    document = model.model_dump(mode="python")
    if document.get("source_ip") is not None:
        document["source_ip"] = str(document["source_ip"])
    document["received_at"] = datetime.now(timezone.utc)
    return document


def make_token_response(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=settings.access_token_minutes * 60,
    )


@router.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    request: RegisterRequest,
    db: Session = Depends(get_db),
    x_bootstrap_token: str | None = Header(default=None),
) -> User:
    if not settings.bootstrap_token:
        raise HTTPException(status_code=503, detail="Initial account setup is not configured")
    if x_bootstrap_token is None or not hmac.compare_digest(x_bootstrap_token, settings.bootstrap_token):
        raise HTTPException(status_code=401, detail="Invalid bootstrap token")
    if db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(status_code=409, detail="Initial account has already been created")

    user = User(id=1, username=request.username, hashed_password=hash_password(request.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username is already registered") from None
    db.refresh(user)
    return user


LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = Lock()


def login_throttled(client_ip: str) -> bool:
    """Limits attempts per client; the single admin account must not be brute-forceable."""
    now = time.monotonic()
    window = settings.login_window_seconds
    with LOGIN_ATTEMPTS_LOCK:
        recent = [stamp for stamp in LOGIN_ATTEMPTS.get(client_ip, []) if now - stamp < window]
        recent.append(now)
        LOGIN_ATTEMPTS[client_ip] = recent
        if len(LOGIN_ATTEMPTS) > 1000:
            for key in [key for key, stamps in LOGIN_ATTEMPTS.items() if not stamps or now - stamps[-1] >= window]:
                LOGIN_ATTEMPTS.pop(key, None)
        return len(recent) > settings.login_max_per_window


def locked_seconds_remaining(user: User, now: datetime) -> int:
    if user.locked_until is None:
        return 0
    locked_until = user.locked_until if user.locked_until.tzinfo else user.locked_until.replace(tzinfo=timezone.utc)
    return max(0, int((locked_until - now).total_seconds()))


@router.post("/auth/login", response_model=TokenResponse)
def login(request: LoginRequest, http_request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    client_ip = http_request.client.host if http_request.client else "unknown"
    if login_throttled(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts",
            headers={"Retry-After": str(settings.login_window_seconds)},
        )

    now = datetime.now(timezone.utc)
    user = db.scalar(select(User).where(User.username == request.username))
    remaining = locked_seconds_remaining(user, now) if user is not None else 0
    if remaining:
        raise HTTPException(
            status_code=429,
            detail="Account temporarily locked",
            headers={"Retry-After": str(remaining)},
        )

    if user is None or not verify_password(request.password, user.hashed_password) or not user.is_active:
        if user is not None:
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= settings.login_max_attempts:
                user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
                user.failed_attempts = 0
            db.commit()
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.failed_attempts or user.locked_until is not None:
        user.failed_attempts = 0
        user.locked_until = None
        db.commit()
    return make_token_response(user)


@router.get("/auth/me", response_model=UserResponse)
def read_current_user(user: User = Depends(get_current_user)) -> User:
    return user


@router.put("/auth/me", response_model=UserResponse)
def update_current_user(
    request: AccountUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> User:
    if not verify_password(request.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if request.new_username is not None:
        user.username = request.new_username
    if request.new_password is not None:
        user.hashed_password = hash_password(request.new_password)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username is already registered") from None
    db.refresh(user)
    return user


@router.post("/collector/metrics", status_code=status.HTTP_202_ACCEPTED)
def ingest_metrics(
    metric: MetricsIn,
    db: Session = Depends(get_db),
    _: None = Depends(require_collector_key),
) -> dict[str, str]:
    mongo_collection("metrics").insert_one(as_document(metric))
    hub.publish(
        "metrics",
        {
            "host": metric.host,
            "timestamp": iso_timestamp(metric.timestamp),
            "cpu_percent": metric.cpu_percent,
            "memory_percent": metric.memory_percent,
            "disk_percent": metric.disk_percent,
        },
    )
    queue_alert_dispatch()
    return {"status": "accepted"}


@router.post("/collector/security-events", status_code=status.HTTP_202_ACCEPTED)
def ingest_security_event(
    event: SecurityEventIn, _: None = Depends(require_collector_key)
) -> dict[str, str]:
    mongo_collection("security_events").insert_one(as_document(event))
    hub.publish(
        "security_events",
        {"host": event.host, "timestamp": iso_timestamp(event.timestamp), "category": event.category},
    )
    return {"status": "accepted"}


@router.post("/collector/scan-results", status_code=status.HTTP_202_ACCEPTED)
def ingest_scan_result(
    result: ScanResultIn, _: None = Depends(require_collector_key)
) -> dict[str, str]:
    mongo_collection("scan_results").insert_one(as_document(result))
    hub.publish(
        "scan_results",
        {"host": result.host, "timestamp": iso_timestamp(result.timestamp), "scan_type": result.scan_type},
    )
    return {"status": "accepted"}


def mark_utc(value: Any) -> Any:
    """MongoDB returns naive datetimes that are really UTC; say so in responses.

    Without the offset a browser reads ``2026-11-13T11:44:32`` as local time, which
    shows wrong clock times and makes a fresh sample look stale.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if isinstance(value, dict):
        return {key: mark_utc(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mark_utc(item) for item in value]
    return value


def iso_timestamp(value: Any) -> Any:
    """Keeps ISO-8601's ``T`` so browsers parse the time as UTC, not local."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def host_scope(host: str) -> dict[str, Any]:
    if host == DEFAULT_HOST:
        return {"$or": [{"host": DEFAULT_HOST}, {"host": {"$exists": False}}]}
    return {"host": host}


def host_summaries(hours: int) -> list[dict[str, Any]]:
    window_start = datetime.now(timezone.utc) - timedelta(hours=hours)
    pipeline = [
        {"$match": {"timestamp": {"$gte": window_start}}},
        {"$sort": {"timestamp": -1}},
        {
            "$group": {
                "_id": {"$ifNull": ["$host", DEFAULT_HOST]},
                "metrics": {"$first": "$$ROOT"},
            }
        },
    ]
    summaries: list[dict[str, Any]] = []
    for row in mongo_collection("metrics").aggregate(pipeline):
        metrics = row.get("metrics") or {}
        metrics.pop("_id", None)
        summaries.append({"host": row.get("_id") or DEFAULT_HOST, "metrics": metrics})
    summaries.sort(key=lambda item: str(item["metrics"].get("timestamp") or ""), reverse=True)
    return summaries


def alerts_for_host(
    host: str,
    metrics: dict[str, Any],
    events: list[dict[str, Any]],
    scans: list[dict[str, Any]],
    thresholds: SystemThreshold | HostThreshold,
    rules: list[AlertRule],
    whitelist_ips: set[str],
    failed_logins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    active_alerts: list[dict[str, Any]] = []

    unique_security_events: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        if event.get("severity") not in {"high", "critical"}:
            continue
        if event.get("category") == "unknown_listening_port":
            continue
        details = event.get("details") or {}
        key = (
            event.get("category"),
            event.get("source_ip"),
            details.get("path"),
            details.get("port"),
            event.get("message"),
        )
        unique_security_events.setdefault(key, event)
    active_alerts.extend(
        {"host": host, "category": "security_event", "event": event}
        for event in unique_security_events.values()
    )

    latest_port_scan = next(
        (scan for scan in scans if scan.get("scan_type") == "open_ports"),
        None,
    )
    if latest_port_scan:
        port_findings = latest_port_scan.get("findings", [])
        if any(finding.get("status") == "unavailable" for finding in port_findings):
            active_alerts.append({"host": host, "category": "port_scan_unavailable"})
        else:
            unapproved_ports: dict[int, dict[str, Any]] = {}
            for finding in port_findings:
                port = finding.get("port")
                if finding.get("known") is False and isinstance(port, int) and not isinstance(port, bool):
                    unapproved_ports.setdefault(port, finding)
            active_alerts.extend(
                {
                    "host": host,
                    "category": "unknown_listening_port",
                    "port": port,
                    "process": finding.get("process", "unknown"),
                }
                for port, finding in unapproved_ports.items()
            )

    for metric, threshold in (
        ("cpu_percent", thresholds.cpu_percent),
        ("memory_percent", thresholds.memory_percent),
        ("disk_percent", thresholds.disk_percent),
    ):
        value = metrics.get(metric)
        if isinstance(value, (int, float)) and value >= threshold:
            active_alerts.append(
                {"host": host, "category": "system_threshold", "metric": metric, "value": value}
            )

    comparison = {
        ">": lambda value, threshold: value > threshold,
        ">=": lambda value, threshold: value >= threshold,
        "<": lambda value, threshold: value < threshold,
        "<=": lambda value, threshold: value <= threshold,
        "==": lambda value, threshold: value == threshold,
    }
    for rule in rules:
        value = metrics.get(rule.metric)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and comparison[rule.operator](value, rule.threshold)
        ):
            active_alerts.append(
                {
                    "host": host,
                    "category": "custom_rule",
                    "rule_id": rule.id,
                    "name": rule.name,
                    "value": value,
                }
            )

    for login_counts in failed_logins:
        source_ip = login_counts.get("source_ip")
        count = login_counts["count"]
        if source_ip and source_ip not in whitelist_ips and count >= thresholds.failed_login_count:
            active_alerts.append(
                {
                    "host": host,
                    "category": "failed_login_threshold",
                    "source_ip": source_ip,
                    "count": count,
                }
            )
    return active_alerts


def dashboard_events(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keeps rare authentication events from being crowded out by high-volume port events."""
    logins = [event for event in events if event.get("category") == "failed_login"][:limit]
    others = [event for event in events if event.get("category") != "failed_login"][: max(0, limit - len(logins))]
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(logins + others, key=lambda event: event.get("timestamp") or floor, reverse=True)


def balanced_by(items: list[dict[str, Any]], limit: int, key: str) -> list[dict[str, Any]]:
    """Interleaves one item per group so every group survives the row limit."""
    buckets: dict[Any, list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(item.get(key), []).append(item)
    selected: list[dict[str, Any]] = []
    while len(selected) < limit and any(buckets.values()):
        for bucket in buckets.values():
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
    return selected


def latest_scan_copies(window_scans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepends the newest scan of each host and type.

    Slow scans (package audits run every few hours) would otherwise be filtered out of
    the dashboard whenever the selected range is shorter than the scan interval.
    """
    latest = list(
        mongo_collection("scan_results").aggregate(
            [
                {"$sort": {"scan_type": 1, "timestamp": -1}},
                {
                    "$group": {
                        "_id": {"scan_type": "$scan_type", "host": "$host"},
                        "doc": {"$first": "$$ROOT"},
                    }
                },
                {"$replaceRoot": {"newRoot": "$doc"}},
                {"$project": {"_id": 0}},
            ]
        )
    )
    known = {(scan.get("host"), scan.get("scan_type"), scan.get("timestamp")) for scan in window_scans}
    fresh = [
        scan
        for scan in latest
        if (scan.get("host"), scan.get("scan_type"), scan.get("timestamp")) not in known
    ]
    return fresh + window_scans


def dashboard_snapshot(db: Session, hours: int = 24, host: str | None = None) -> dict[str, Any]:
    window_start = datetime.now(timezone.utc) - timedelta(hours=hours)
    summaries = host_summaries(hours)
    available_hosts = [summary["host"] for summary in summaries]
    selected_host = host if host else (available_hosts[0] if available_hosts else None)

    events = list(
        mongo_collection("security_events")
        .find({"timestamp": {"$gte": window_start}}, {"_id": 0})
        .sort("timestamp", -1)
        .limit(500)
    )
    # Port scans run every cycle, so a single newest-N query hides the other scan types
    # (package audits run a few times a day) once the window holds a few hundred scans.
    scans: list[dict[str, Any]] = []
    for scan_type in ("open_ports", "file_integrity", "failed_logins", "package_audit"):
        scans.extend(
            mongo_collection("scan_results")
            .find({"timestamp": {"$gte": window_start}, "scan_type": scan_type}, {"_id": 0})
            .sort("timestamp", -1)
            .limit(50)
        )
    scans = latest_scan_copies(scans)
    for event in events:
        event["host"] = event.get("host") or DEFAULT_HOST
    for scan in scans:
        scan["host"] = scan.get("host") or DEFAULT_HOST

    # Authentication failures arrive rarely and are drowned out by thousands of listening
    # port events, so read them directly instead of trusting the newest-N window above.
    login_events = list(
        mongo_collection("security_events")
        .find({"timestamp": {"$gte": window_start}, "category": "failed_login"}, {"_id": 0})
        .sort("timestamp", -1)
        .limit(100)
    )
    known_events = {
        (event.get("host"), event.get("timestamp"), event.get("category"), str(event.get("details")))
        for event in events
    }
    for event in login_events:
        event["host"] = event.get("host") or DEFAULT_HOST
        identity = (event.get("host"), event.get("timestamp"), event.get("category"), str(event.get("details")))
        if identity not in known_events:
            events.append(event)
            known_events.add(identity)

    if selected_host:
        metrics = next(
            (summary["metrics"] for summary in summaries if summary["host"] == selected_host),
            None,
        )
    else:
        metrics = mongo_collection("metrics").find_one(sort=[("timestamp", -1)])
        if metrics:
            metrics.pop("_id", None)

    global_thresholds = get_or_create_thresholds(db)
    overrides = {row.host: row for row in db.scalars(select(HostThreshold))}
    rules = list(db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))))
    whitelist_ips = set(db.scalars(select(IPWhitelist.ip_address)))

    if host:
        evaluation_hosts = [host]
    else:
        # ``unassigned`` only holds samples stored before collectors sent a host id; once a
        # real host reports, evaluating the legacy bucket duplicates every alert.
        real_hosts = [name for name in available_hosts if name != DEFAULT_HOST]
        evaluation_hosts = real_hosts or available_hosts
    active_alerts: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for evaluation_host in evaluation_hosts:
        host_metrics = next(
            (summary["metrics"] for summary in summaries if summary["host"] == evaluation_host),
            {},
        )
        scoped_thresholds = overrides.get(evaluation_host) or global_thresholds
        scoped_rules = [rule for rule in rules if not rule.host or rule.host == evaluation_host]
        cutoff = now - timedelta(minutes=scoped_thresholds.failed_login_window_minutes)
        failed_logins = [
            {"host": evaluation_host, "source_ip": row.get("_id"), "count": row["count"]}
            for row in mongo_collection("security_events").aggregate(
                [
                    {
                        "$match": {
                            "category": "failed_login",
                            "timestamp": {"$gte": cutoff},
                            **host_scope(evaluation_host),
                        }
                    },
                    {"$group": {"_id": "$source_ip", "count": {"$sum": 1}}},
                ]
            )
        ]
        active_alerts.extend(
            alerts_for_host(
                evaluation_host,
                host_metrics,
                [event for event in events if event["host"] == evaluation_host],
                [scan for scan in scans if scan["host"] == evaluation_host],
                scoped_thresholds,
                scoped_rules,
                whitelist_ips,
                failed_logins,
            )
        )

    selected_events = (
        [event for event in events if event["host"] == selected_host] if selected_host else events
    )
    selected_scans = (
        [scan for scan in scans if scan["host"] == selected_host] if selected_host else scans
    )
    return {
        "metrics": metrics,
        "selected_host": selected_host,
        "hosts": [
            {
                "host": summary["host"],
                "last_seen": summary["metrics"].get("timestamp"),
                "cpu_percent": summary["metrics"].get("cpu_percent"),
                "memory_percent": summary["metrics"].get("memory_percent"),
                "disk_percent": summary["metrics"].get("disk_percent"),
            }
            for summary in summaries
        ],
        "security_events": dashboard_events(selected_events, 100),
        "scan_results": balanced_by(selected_scans, 100, "scan_type"),
        "active_alerts": len(active_alerts),
        "alert_details": active_alerts,
    }


@router.get("/dashboard/hosts")
def dashboard_hosts(
    hours: int = Query(default=24, ge=1, le=720),
    _: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return [
        {
            "host": summary["host"],
            "last_seen": summary["metrics"].get("timestamp"),
            "cpu_percent": summary["metrics"].get("cpu_percent"),
            "memory_percent": summary["metrics"].get("memory_percent"),
            "disk_percent": summary["metrics"].get("disk_percent"),
        }
        for summary in host_summaries(hours)
    ]


@router.get("/dashboard/latest")
def latest_dashboard(
    db: Session = Depends(get_db),
    hours: int = Query(default=24, ge=1, le=720),
    host: str | None = Query(default=None, max_length=80),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    return mark_utc(dashboard_snapshot(db, hours, host))


@router.get("/dashboard/history")
def dashboard_history(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=1000, ge=1, le=5000),
    host: str | None = Query(default=None, max_length=80),
    _: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    scope_host = host
    if not scope_host:
        latest = mongo_collection("metrics").find_one(sort=[("timestamp", -1)])
        if latest:
            scope_host = latest.get("host") or DEFAULT_HOST
    query: dict[str, Any] = {"timestamp": {"$gte": start}}
    if scope_host:
        query = {"$and": [query, host_scope(scope_host)]}
    cursor = (
        mongo_collection("metrics")
        .find(query, {"_id": 0})
        .sort("timestamp", 1)
        .limit(limit)
    )
    return mark_utc(list(cursor))


@router.get("/whitelist", response_model=list[IPWhitelistResponse])
def list_whitelist(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[IPWhitelist]:
    return list(db.scalars(select(IPWhitelist).order_by(IPWhitelist.ip_address)))


@router.post("/whitelist", response_model=IPWhitelistResponse, status_code=status.HTTP_201_CREATED)
def add_whitelist_ip(
    request: IPWhitelistCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> IPWhitelist:
    entry = IPWhitelist(ip_address=str(request.ip_address), label=request.label)
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="IP address is already whitelisted") from None
    db.refresh(entry)
    return entry


@router.delete("/whitelist/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_whitelist_ip(
    entry_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    entry = db.get(IPWhitelist, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Whitelist entry not found")
    db.delete(entry)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/alert-rules", response_model=list[AlertRuleResponse])
def list_alert_rules(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[AlertRule]:
    return list(db.scalars(select(AlertRule).order_by(AlertRule.id)))


@router.post("/alert-rules", response_model=AlertRuleResponse, status_code=status.HTTP_201_CREATED)
def create_alert_rule(
    request: AlertRuleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AlertRule:
    rule = AlertRule(**request.model_dump(), created_by_id=user.id)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/alert-rules/{rule_id}", response_model=AlertRuleResponse)
def update_alert_rule(
    rule_id: int,
    request: AlertRuleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    for field, value in request.model_dump().items():
        setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/alert-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    db.delete(rule)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def effective_thresholds(db: Session, host: str | None) -> dict[str, Any]:
    override = None
    if host:
        override = db.scalar(select(HostThreshold).where(HostThreshold.host == host))
    source = override or get_or_create_thresholds(db)
    return {
        "scope": host or "",
        "overridden": override is not None,
        "cpu_percent": source.cpu_percent,
        "memory_percent": source.memory_percent,
        "disk_percent": source.disk_percent,
        "failed_login_count": source.failed_login_count,
        "failed_login_window_minutes": source.failed_login_window_minutes,
    }


@router.get("/thresholds", response_model=EffectiveThresholds)
def get_thresholds(
    host: str | None = Query(default=None, max_length=80),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    return effective_thresholds(db, host)


@router.get("/thresholds/overrides", response_model=list[HostThresholdsResponse])
def list_host_thresholds(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[HostThreshold]:
    return list(db.scalars(select(HostThreshold).order_by(HostThreshold.host)))


@router.put("/thresholds", response_model=EffectiveThresholds)
def update_thresholds(
    request: ThresholdsUpdate,
    host: str | None = Query(default=None, max_length=80),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    if host:
        record = db.scalar(select(HostThreshold).where(HostThreshold.host == host))
        if record is None:
            db.add(HostThreshold(host=host, **request.model_dump()))
        else:
            for field, value in request.model_dump().items():
                setattr(record, field, value)
    else:
        record = get_or_create_thresholds(db)
        for field, value in request.model_dump().items():
            setattr(record, field, value)
    db.commit()
    return effective_thresholds(db, host)


@router.delete("/thresholds", status_code=status.HTTP_204_NO_CONTENT)
def delete_host_thresholds(
    host: str = Query(..., max_length=80),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    record = db.scalar(select(HostThreshold).where(HostThreshold.host == host))
    if record is None:
        raise HTTPException(status_code=404, detail="No host-specific thresholds for that host")
    db.delete(record)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/retention", response_model=RetentionSettings)
def get_retention(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict[str, int]:
    return retention_days(db)


@router.put("/retention", response_model=RetentionSettings)
def update_retention(
    request: RetentionSettings,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, int]:
    record = retention_record(db)
    record.metrics_days = request.metrics
    record.security_events_days = request.security_events
    record.scan_results_days = request.scan_results
    db.commit()
    try:
        apply_retention(mongo_database, retention_days(db))
    except PyMongoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Retention saved, but MongoDB did not accept the TTL index yet: {exc}",
        ) from exc
    return retention_days(db)


@router.get("/dashboard/stream")
async def stream_dashboard(
    request: Request, _: User = Depends(get_current_user)
) -> StreamingResponse:
    """Server-sent events: one wake-up per ingest burst, then the client refetches."""
    queue = hub.subscribe()

    async def event_source():
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event_name, payload = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {event_name}\ndata: {json.dumps(payload, default=str)}\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def notification_host_name() -> str:
    try:
        return socket.gethostname() or "unknown-host"
    except OSError:
        return "unknown-host"


_ALERT_CYCLE_LOCK = Lock()


def queue_alert_dispatch() -> None:
    Thread(target=process_alerts, daemon=True).start()


def process_alerts() -> None:
    try:
        with _ALERT_CYCLE_LOCK:
            run_alert_cycle()
    except Exception:
        # Background alert work must never affect collector ingestion; the next sample retries.
        pass


def run_alert_cycle() -> None:
    db = SessionLocal()
    cycle_started = datetime.now(timezone.utc)
    try:
        snapshot = dashboard_snapshot(db)
        metrics = snapshot.get("metrics") or {}
        alerts = snapshot.get("alert_details") or []
        new_alerts = sync_alerts(db, alerts)
        resolve_stale_alerts(db, cycle_started)
        channels = list(
            db.scalars(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))
        )
    finally:
        db.close()

    if not new_alerts or not channels:
        return

    alert_hosts = {str(alert.get("host")) for alert in new_alerts if alert.get("host")}
    host_name = alert_hosts.pop() if len(alert_hosts) == 1 else "fleet"
    payload = build_alert_payload(new_alerts, metrics=metrics, host_name=host_name)
    for channel in channels:
        send_to_channel(channel, payload)


def resolve_stale_alerts(db: Session, cycle_started: datetime) -> int:
    """Mark alerts resolved once a full cycle no longer reports them as active."""
    stale: list[Alert] = []
    for record in db.scalars(select(Alert).where(Alert.status != "resolved")):
        last_seen = record.last_seen
        if last_seen is None:
            continue
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen < cycle_started:
            stale.append(record)
    if not stale:
        return 0
    now = datetime.now(timezone.utc)
    for record in stale:
        record.status = "resolved"
        record.resolved_at = now
    db.commit()
    return len(stale)


def sync_alerts(db: Session, alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    created: list[dict[str, Any]] = []
    seen: set[str] = set()
    for alert in alerts:
        fingerprint = alert_fingerprint(alert)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        message = alert_message(alert)[:500]
        details = json_safe(alert)
        record = db.scalar(select(Alert).where(Alert.fingerprint == fingerprint))
        if record is None:
            record = Alert(
                fingerprint=fingerprint,
                host=str(alert.get("host") or DEFAULT_HOST)[:80],
                category=str(alert.get("category", "unknown"))[:80],
                severity=alert_severity(alert),
                message=message,
                details=details,
                status="new",
                first_seen=now,
                last_seen=now,
            )
            db.add(record)
            created.append(details)
        else:
            record.last_seen = now
            record.message = message
            record.details = details
            if record.status == "resolved":
                record.status = "new"
                record.acknowledged_at = None
                record.acknowledged_by_id = None
                record.resolved_at = None
                created.append(details)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return []
    return created


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def send_to_channel(channel: NotificationChannel, payload: dict[str, Any]) -> None:
    send_alert_webhook(
        channel.webhook_url, channel_body(channel.kind, payload, channel.chat_id), channel.kind
    )


def channel_or_404(db: Session, channel_id: int) -> NotificationChannel:
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    return channel


@router.get("/notification-channels", response_model=list[NotificationChannelResponse])
def list_notification_channels(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[NotificationChannel]:
    return list(db.scalars(select(NotificationChannel).order_by(NotificationChannel.id)))


@router.post(
    "/notification-channels",
    response_model=NotificationChannelResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_notification_channel(
    request: NotificationChannelCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> NotificationChannel:
    channel = NotificationChannel(**request.model_dump())
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


@router.put("/notification-channels/{channel_id}", response_model=NotificationChannelResponse)
def update_notification_channel(
    channel_id: int,
    request: NotificationChannelUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> NotificationChannel:
    channel = channel_or_404(db, channel_id)
    for field, value in request.model_dump(exclude_none=True).items():
        setattr(channel, field, value)
    if channel.kind == "telegram" and not channel.chat_id:
        db.rollback()
        raise HTTPException(status_code=422, detail="Telegram channels require a chat ID")
    db.commit()
    db.refresh(channel)
    return channel


@router.delete("/notification-channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    channel = channel_or_404(db, channel_id)
    db.delete(channel)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/notification-channels/{channel_id}/test")
def test_notification_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, bool]:
    channel = channel_or_404(db, channel_id)
    payload = build_alert_payload(
        [{"category": "system_threshold", "metric": "cpu_percent", "value": 91}],
        metrics={"cpu_percent": 91, "memory_percent": 64, "disk_percent": 52},
        host_name=notification_host_name(),
    )
    if not send_alert_webhook(
        channel.webhook_url, channel_body(channel.kind, payload, channel.chat_id), channel.kind
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Notification delivery failed; check the webhook URL and Telegram chat ID",
        )
    return {"sent": True}


@router.get("/alerts", response_model=list[AlertResponse])
def list_alerts(
    status_filter: str | None = Query(default=None, alias="status"),
    host: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Alert]:
    query = select(Alert)
    if status_filter in {"new", "acknowledged", "resolved"}:
        query = query.where(Alert.status == status_filter)
    if host:
        query = query.where(Alert.host == host)
    query = query.order_by(Alert.last_seen.desc()).limit(limit)
    return list(db.scalars(query))


@router.put("/alerts/{alert_id}", response_model=AlertResponse)
def update_alert_status(
    alert_id: int,
    request: AlertStatusUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Alert:
    record = db.get(Alert, alert_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    now = datetime.now(timezone.utc)
    record.status = request.status
    if request.status == "acknowledged":
        record.acknowledged_at = now
        record.acknowledged_by_id = user.id
        record.resolved_at = None
    elif request.status == "resolved":
        record.resolved_at = now
    else:
        record.acknowledged_at = None
        record.acknowledged_by_id = None
        record.resolved_at = None
    db.commit()
    db.refresh(record)
    return record


def get_or_create_thresholds(db: Session) -> SystemThreshold:
    thresholds = db.get(SystemThreshold, 1)
    if thresholds is None:
        thresholds = SystemThreshold(id=1)
        db.add(thresholds)
        db.commit()
        db.refresh(thresholds)
    return thresholds