import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, StringConstraints, model_validator
from typing_extensions import Annotated

from .notifications import DEFAULT_HOST, validate_webhook_url

Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=80)]
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]


class RegisterRequest(BaseModel):
    username: Username
    password: Password


class LoginRequest(BaseModel):
    username: Username
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class AccountUpdateRequest(BaseModel):
    current_password: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    new_username: Username | None = None
    new_password: Password | None = None

    @model_validator(mode="after")
    def require_account_change(self):
        if self.new_username is None and self.new_password is None:
            raise ValueError("Provide a new username or password")
        return self


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: int
    username: str
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


HostName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class MetricsIn(BaseModel):
    host: HostName = DEFAULT_HOST
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    disk_percent: float = Field(ge=0, le=100)
    network_bytes_sent: int = Field(default=0, ge=0)
    network_bytes_received: int = Field(default=0, ge=0)
    cpu_temp_celsius: float | None = None
    system_temp_celsius: float | None = None


class SecurityEventIn(BaseModel):
    host: HostName = DEFAULT_HOST
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    source_ip: IPvAnyAddress | None = None
    message: Annotated[str, StringConstraints(max_length=2000)] = ""
    details: dict[str, Any] = Field(default_factory=dict)


class ScanResultIn(BaseModel):
    host: HostName = DEFAULT_HOST
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    scan_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class IPWhitelistCreate(BaseModel):
    ip_address: IPvAnyAddress
    label: Annotated[str, StringConstraints(max_length=120)] = ""


class IPWhitelistResponse(BaseModel):
    id: int
    ip_address: str
    label: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertStatusUpdate(BaseModel):
    status: Literal["new", "acknowledged", "resolved"]


class AlertResponse(BaseModel):
    id: int
    host: str
    category: str
    severity: str
    message: str
    status: str
    details: dict[str, Any]
    first_seen: datetime
    last_seen: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    acknowledged_by_id: int | None

    model_config = ConfigDict(from_attributes=True)


NotificationKind = Literal["slack", "discord", "telegram", "webhook"]
TELEGRAM_TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{10,}$")


def telegram_url(value: str) -> str:
    """Accepts the bare bot token from BotFather as well as a full sendMessage URL."""
    if TELEGRAM_TOKEN.match(value):
        return f"https://api.telegram.org/bot{value}/sendMessage"
    return value


class NotificationChannelCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    kind: NotificationKind
    webhook_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    chat_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_channel(self):
        self.webhook_url = telegram_url(self.webhook_url)
        validate_webhook_url(self.webhook_url)
        if self.kind == "telegram" and not self.chat_id:
            raise ValueError("Telegram channels require a chat ID")
        return self


class NotificationChannelUpdate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] | None = None
    kind: NotificationKind | None = None
    webhook_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)] | None = None
    chat_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_webhook(self):
        if self.webhook_url is not None:
            self.webhook_url = telegram_url(self.webhook_url)
            validate_webhook_url(self.webhook_url)
        return self


class NotificationChannelResponse(BaseModel):
    id: int
    name: str
    kind: str
    url_hint: str
    chat_id: str | None
    enabled: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertRuleCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    host: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    metric: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    operator: Literal[">", ">=", "<", "<=", "=="]
    threshold: float
    enabled: bool = True


class AlertRuleUpdate(AlertRuleCreate):
    pass


class AlertRuleResponse(AlertRuleCreate):
    id: int
    created_by_id: int

    model_config = ConfigDict(from_attributes=True)


class RetentionSettings(BaseModel):
    metrics: Annotated[int, Field(ge=0, le=3650)]
    security_events: Annotated[int, Field(ge=0, le=3650)]
    scan_results: Annotated[int, Field(ge=0, le=3650)]


class ThresholdsUpdate(BaseModel):
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    disk_percent: float = Field(ge=0, le=100)
    failed_login_count: int = Field(ge=1, le=10000)
    failed_login_window_minutes: int = Field(ge=1, le=1440)


class ThresholdsResponse(ThresholdsUpdate):
    id: int

    model_config = ConfigDict(from_attributes=True)


class EffectiveThresholds(ThresholdsUpdate):
    scope: str
    overridden: bool


class HostThresholdsResponse(ThresholdsUpdate):
    host: str

    model_config = ConfigDict(from_attributes=True)