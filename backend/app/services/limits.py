"""Usage limits: the abuse and cost controls. Deterministic code; no model is ever involved.

Each limit counts requests per key in fixed windows aligned to the Unix epoch, in UTC: an hour
starts on the hour, a day at 00:00 UTC. One statement both checks and counts:

    INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 WHERE count < max RETURNING count

so a limit holds under any number of concurrent requests, across processes, and survives restarts.
A request over the limit is refused and not counted. Model-backed work is charged when it is
admitted, whatever the provider then does, because the cost is incurred either way; a session's
deletion never gives anything back.
"""

import hashlib
import ipaddress
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.usage_counter import UsageCounter

logger = logging.getLogger(__name__)

DAY = timedelta(days=1)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Limit:
    scope: str
    max: int
    window: timedelta


# Per network (before anyone is signed in): counted before any password is hashed.
REGISTER = Limit("register_ip", 5, timedelta(hours=1))
LOGIN = Limit("login_ip", 20, timedelta(minutes=15))
FORGOT_PASSWORD = Limit("forgot_password_ip", 5, timedelta(hours=1))
RESEND_CONFIRMATION = Limit("resend_confirmation_ip", 5, timedelta(hours=1))
# Per candidate, per UTC day.
RESUME_UPLOAD = Limit("resume_upload", 10, DAY)
AI_EXTRACTION = Limit("ai_extraction", 5, DAY)
AI_PLANNING = Limit("ai_planning", 10, DAY)
AI_WORDING = Limit("ai_wording", 150, DAY)
AI_EVALUATION = Limit("ai_evaluation", 150, DAY)
GLOBAL_KEY = "all"


def ai_global(settings: Settings) -> Limit:
    """Every admitted model-backed request of every candidate: the circuit breaker that new
    accounts cannot get around. Sized from the provider plan (AI_DAILY_REQUEST_LIMIT)."""
    return Limit("ai_global", settings.ai_daily_request_limit, DAY)


class LimitReached(Exception):
    def __init__(self, scope: str, retry_after: int) -> None:
        super().__init__(scope)
        self.scope, self.retry_after = scope, retry_after  # seconds until the window ends


def window_start(now: datetime, window: timedelta) -> datetime:
    return EPOCH + ((now - EPOCH) // window) * window


def consume(db: Session, limit: Limit, key: str, now: datetime | None = None) -> None:
    """Counts one request, or raises LimitReached and counts nothing. The caller commits."""
    now = now or datetime.now(UTC)
    start = window_start(now, limit.window)
    end = start + limit.window
    db.execute(delete(UsageCounter).where(UsageCounter.expires_at <= now))  # ended windows
    counted = limit.max > 0 and db.execute(
        insert(UsageCounter)
        .values(scope=limit.scope, key=key, window_start=start, count=1, expires_at=end)
        .on_conflict_do_update(
            index_elements=[UsageCounter.scope, UsageCounter.key, UsageCounter.window_start],
            set_={"count": UsageCounter.count + 1},
            where=UsageCounter.count < limit.max,
        )
        .returning(UsageCounter.count)
    ).first() is not None
    if not counted:
        logger.info("Usage limit %s reached", limit.scope)  # the scope only, never the key
        raise LimitReached(limit.scope, max(1, math.ceil((end - now).total_seconds())))


def network_key(host: str | None) -> str:
    """The key for a client address: an IPv4 address, or the /64 network of an IPv6 address
    (one subscriber usually holds a whole /64). Hashed so the table never shows an address;
    anything that is not an address shares one key."""
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return hashlib.sha256(b"net:unknown").hexdigest()
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    network = ip if ip.version == 4 else ipaddress.ip_network(f"{ip}/64", strict=False)
    return hashlib.sha256(f"net:{network}".encode()).hexdigest()


def charge_ai(db: Session, limit: Limit, candidate_id: uuid.UUID, settings: Settings) -> None:
    """Admits one model-backed request: charged to the candidate's daily limit and to the global
    budget, then committed, so the charge stands whatever happens to the request. On
    LimitReached nothing is charged."""
    try:
        consume(db, limit, str(candidate_id))
        consume(db, ai_global(settings), GLOBAL_KEY)
    except LimitReached:
        db.rollback()
        raise
    db.commit()


def admit_ai(db: Session, limit: Limit, candidate_id: uuid.UUID, settings: Settings) -> bool:
    """charge_ai for work that has a deterministic fallback: False means use the fallback."""
    try:
        charge_ai(db, limit, candidate_id, settings)
    except LimitReached:
        return False
    return True
