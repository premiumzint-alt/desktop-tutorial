from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

DEFAULT_HOST = "unassigned"


def validate_webhook_url(url: str) -> str:
    if not url or not url.strip():
        raise ValueError("Webhook URL is required")
    value = url.strip()
    if not value.startswith(("http://", "https://")):
        raise ValueError("Webhook URL must start with http:// or https://")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Webhook URL is invalid")
    return value


def _alert_message(alert: dict[str, Any]) -> str:
    category = alert.get("category", "unknown")
    if category == "unknown_listening_port":
        return f"Unapproved listening port {alert.get('port', 'unknown')} on {alert.get('process', 'unknown')}"
    if category == "failed_login_threshold":
        return f"Failed logins from {alert.get('source_ip', 'unknown')} reached {alert.get('count', 0)}"
    if category == "security_event":
        return alert.get("event", {}).get("message") or alert.get("event", {}).get("category") or "Security event detected"
    if category == "system_threshold":
        metric = alert.get("metric", "system")
        value = alert.get("value", 0)
        return f"{metric} reached {value}"
    if category == "custom_rule":
        return f"Custom rule {alert.get('name', 'alert')} reached {alert.get('value', 0)}"
    if category == "port_scan_unavailable":
        return "Port scan data is unavailable"
    return f"Alert triggered: {category}"


def build_alert_payload(
    alerts: list[dict[str, Any]],
    metrics: dict[str, Any] | None = None,
    host_name: str | None = None,
    source: str = "Sentinel",
) -> dict[str, Any]:
    normalized = []
    for index, alert in enumerate(alerts or [], start=1):
        normalized.append(
            {
                "id": index,
                "category": alert.get("category", "unknown"),
                "message": _alert_message(alert),
                "details": alert,
            }
        )

    critical_count = sum(1 for alert in normalized if alert_severity(alert["details"]) == "critical")
    summary = {
        "total_alerts": len(normalized),
        "critical_count": critical_count,
        "warning_count": max(len(normalized) - critical_count, 0),
        "status": "critical" if critical_count else ("warning" if normalized else "healthy"),
    }
    return {
        "source": source,
        "host": host_name or "unknown-host",
        "summary": summary,
        "metrics": metrics or {},
        "alerts": normalized,
    }


_LAST_ALERT_DIGEST: dict[str, str] = {}


DELIVERY_ERROR_MARKERS = {
    "invalid_token",
    "invalid_payload",
    "no_service",
    "no_text",
    "no_channel",
    "channel_not_found",
    "action_prohibited",
}


def delivery_body_is_error(raw: bytes) -> bool:
    """Flag success-shaped responses that actually report a delivery failure.

    Slack answers a bad token with HTTP 200 and the plain body ``invalid_token``,
    so the status code alone is not enough to claim the message was delivered.
    """
    text = raw.decode("utf-8", "ignore").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered in DELIVERY_ERROR_MARKERS:
        return True
    if lowered.startswith("{"):
        try:
            data = json.loads(lowered)
        except ValueError:
            return False
        if isinstance(data, dict):
            return data.get("ok") is False or bool(data.get("error")) or data.get("success") is False
    return False


def delivery_accepted(kind: str | None, status: int, raw: bytes) -> bool:
    """Decide whether a provider really accepted the message.

    Each provider acknowledges differently: Slack replies with the bare text
    ``ok``, Discord with an empty 204, Telegram with ``{"ok": true}``. Relying on
    the status code alone reports failed deliveries as successful.
    """
    if not 200 <= status < 300:
        return False
    text = raw.decode("utf-8", "ignore").strip()
    if kind == "slack":
        return text.lower() == "ok"
    if kind == "telegram":
        try:
            data = json.loads(text or "{}")
        except ValueError:
            return False
        return isinstance(data, dict) and data.get("ok") is True
    return not delivery_body_is_error(raw)


def send_alert_webhook(webhook_url: str, payload: dict[str, Any], kind: str | None = None) -> bool:
    try:
        validate_webhook_url(webhook_url)
    except ValueError:
        return False
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request_obj = request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(request_obj, timeout=8) as response:
            return delivery_accepted(kind, response.status, response.read())
    except (error.HTTPError, error.URLError, TimeoutError, OSError):
        return False


def maybe_send_alert_webhook(webhook_url: str, payload: dict[str, Any]) -> bool:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    key = webhook_url
    if digest == _LAST_ALERT_DIGEST.get(key):
        return True
    success = send_alert_webhook(webhook_url, payload)
    if success:
        _LAST_ALERT_DIGEST[key] = digest
    return success


def alert_message(alert: dict[str, Any]) -> str:
    return _alert_message(alert)


def alert_severity(alert: dict[str, Any]) -> str:
    category = alert.get("category", "unknown")
    if category in {"unknown_listening_port", "failed_login_threshold", "port_scan_unavailable"}:
        return "critical"
    if category == "security_event":
        severity = (alert.get("event") or {}).get("severity")
        return "critical" if severity in {"high", "critical"} else "warning"
    if category in {"system_threshold", "custom_rule"}:
        return "warning"
    return "info"


def alert_fingerprint(alert: dict[str, Any]) -> str:
    category = alert.get("category", "unknown")
    event = alert.get("event") or {}
    event_details = event.get("details") or {}
    parts = [str(alert.get("host") or DEFAULT_HOST), category]
    if category == "system_threshold":
        parts.append(str(alert.get("metric")))
    elif category == "custom_rule":
        parts.append(str(alert.get("rule_id")))
    elif category == "unknown_listening_port":
        parts.append(str(alert.get("port")))
    elif category == "failed_login_threshold":
        parts.append(str(alert.get("source_ip")))
    elif category == "security_event":
        parts.extend(
            [
                str(event.get("category")),
                str(event.get("source_ip")),
                str(event_details.get("path")),
                str(event_details.get("port")),
                str(event.get("message")),
            ]
        )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


def build_service_payload(host: str, message: str, severity: str = "warning") -> dict[str, Any]:
    """Build an alert payload for a service event such as a watchdog restart."""
    state = "critical" if severity == "critical" else "warning"
    return {
        "host": host,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_alerts": 1,
            "critical_count": 1 if state == "critical" else 0,
            "warning_count": 0 if state == "critical" else 1,
            "status": state,
        },
        "alerts": [
            {
                "host": host,
                "category": "service_watchdog",
                "severity": severity,
                "message": message,
            }
        ],
    }


def format_alert_text(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    alerts = payload.get("alerts") or []
    host = payload.get("host") or "unknown-host"
    state = str(summary.get("status", "unknown")).upper()
    lines = [f"Sentinel · {host} · {state} · {summary.get('total_alerts', len(alerts))} alert(s)"]
    for alert in alerts[:10]:
        lines.append(f"• [{alert.get('category', 'unknown')}] {alert.get('message', '')}")
    remaining = len(alerts) - 10
    if remaining > 0:
        lines.append(f"…and {remaining} more")
    return "\n".join(lines)


def channel_body(kind: str, payload: dict[str, Any], chat_id: str | None = None) -> dict[str, Any]:
    text = format_alert_text(payload)
    if kind == "telegram":
        return {"chat_id": chat_id or "", "text": text, "disable_web_page_preview": True}
    if kind == "slack":
        return {"text": text}
    if kind == "discord":
        return {"content": text[:1900]}
    return payload
