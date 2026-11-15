"""Send a watchdog notification through the channels configured in Sentinel.

Called by scripts/watchdog.ps1 after a service restart. The backend directory is
added to the import path and used as the working directory so the SQLite URL
(`sqlite:///./monitor.db`) resolves to the same database the API uses.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a Sentinel watchdog notification")
    parser.add_argument("--message", required=True, help="Message body sent to every enabled channel")
    parser.add_argument("--severity", default="warning", choices=("warning", "critical", "info"))
    parser.add_argument("--host", default=socket.gethostname() or "unassigned")
    args = parser.parse_args()

    backend_dir = Path(__file__).resolve().parents[1] / "backend"
    if not backend_dir.is_dir():
        print("error:backend-not-found")
        return 2
    os.chdir(backend_dir)
    sys.path.insert(0, str(backend_dir))

    from app.database import SessionLocal
    from app.models import NotificationChannel
    from app.notifications import build_service_payload, channel_body, send_alert_webhook
    from sqlalchemy import select

    payload = build_service_payload(args.host, args.message, args.severity)

    db = SessionLocal()
    try:
        channels = list(
            db.scalars(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))
        )
        targets = [(channel.kind, channel.webhook_url, channel.chat_id) for channel in channels]
    finally:
        db.close()

    if not targets:
        print("skip:no-enabled-channels")
        return 0

    delivered = 0
    failed = 0
    for kind, webhook_url, chat_id in targets:
        if send_alert_webhook(webhook_url, channel_body(kind, payload, chat_id), kind):
            delivered += 1
        else:
            failed += 1

    print(f"notified:{delivered} failed:{failed} of:{len(targets)}")
    return 1 if delivered == 0 and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
