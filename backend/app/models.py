from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .notifications import DEFAULT_HOST


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IPWhitelist(Base):
    __tablename__ = "ip_whitelist"

    id: Mapped[int] = mapped_column(primary_key=True)
    ip_address: Mapped[str] = mapped_column(String(45), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), index=True)
    webhook_url: Mapped[str] = mapped_column(String(2000))
    chat_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def url_hint(self) -> str:
        host = urlparse(self.webhook_url).netloc or "configured-endpoint"
        return f"{host}/…{self.webhook_url[-4:]}"


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(80), default=DEFAULT_HOST, index=True)
    category: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    message: Mapped[str] = mapped_column(String(500), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    host: Mapped[str] = mapped_column(String(80), default="", index=True)
    metric: Mapped[str] = mapped_column(String(80), index=True)
    operator: Mapped[str] = mapped_column(String(2))
    threshold: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[User] = relationship()


class HostThreshold(Base):
    __tablename__ = "host_thresholds"

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=90)
    memory_percent: Mapped[float] = mapped_column(Float, default=90)
    disk_percent: Mapped[float] = mapped_column(Float, default=90)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=5)
    failed_login_window_minutes: Mapped[int] = mapped_column(Integer, default=10)


class RetentionSetting(Base):
    __tablename__ = "retention_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    metrics_days: Mapped[int] = mapped_column(Integer, default=7)
    security_events_days: Mapped[int] = mapped_column(Integer, default=30)
    scan_results_days: Mapped[int] = mapped_column(Integer, default=30)


class SystemThreshold(Base):
    __tablename__ = "system_thresholds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cpu_percent: Mapped[float] = mapped_column(Float, default=90)
    memory_percent: Mapped[float] = mapped_column(Float, default=90)
    disk_percent: Mapped[float] = mapped_column(Float, default=90)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=5)
    failed_login_window_minutes: Mapped[int] = mapped_column(Integer, default=10)