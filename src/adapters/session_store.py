"""DynamoDB adapter for the Gestor_de_Sessao (Task 12.2).

Persists conversation sessions and their message history in Amazon DynamoDB,
using DynamoDB's native **Time To Live (TTL)** feature so that expired sessions
are removed automatically by the service (Req 4.3). The adapter is a thin I/O
wrapper around the pure-logic layer in :mod:`src.domain.session`:

* serialization/deserialization between :class:`~src.domain.models.Session` and
  DynamoDB items;
* TTL computation from ``last_activity`` + the configured inactivity timeout;
* resolution of the active session for a ``(user_key, channel)`` pair, reusing
  :func:`src.domain.session.resolve_session` for the active/inactive decision.

Credentials are **never hardcoded**. They are read at runtime from AWS Secrets
Manager (Req 10.1) via :func:`load_aws_credentials` / :meth:`SessionStore.from_secrets_manager`.

The ``boto3`` import is guarded so this module imports cleanly even when the AWS
SDK is not installed (e.g. in CI running only the pure-logic tests). All AWS
interaction goes through injected clients/resources, which makes the adapter
unit-testable with mocks (``moto`` or hand-rolled fakes) without calling real
AWS.

Expected DynamoDB table schema
------------------------------
* Partition key: ``session_id`` (String).
* TTL attribute: ``ttl`` (Number, epoch seconds) — enable TTL on this attribute.
* Global secondary index :data:`USER_CHANNEL_INDEX` with partition key
  ``user_key`` (String) and sort key ``channel`` (String), used to resolve the
  active session for a user on a channel (Req 4.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.domain.config import DEFAULT_TIMEOUT_MIN
from src.domain.models import Message, Session
from src.domain.session import expire_session, resolve_session

# --- Guarded boto3 import -------------------------------------------------------
# The module must import even when boto3 is absent; AWS objects are always
# injected, so the names below are only needed for the convenience factories.
try:  # pragma: no cover - trivial import guard
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without boto3 installed
    boto3 = None  # type: ignore[assignment]
    Config = None  # type: ignore[assignment]

    class BotoCoreError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    class ClientError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    _BOTO3_AVAILABLE = False


#: Name of the GSI used to look up sessions by (user_key, channel) (Req 4.4).
USER_CHANNEL_INDEX: str = "UserChannelIndex"


class SessionStoreError(Exception):
    """Raised when a DynamoDB operation on the session store fails."""


def boto3_available() -> bool:
    """Return whether the AWS SDK (boto3) is importable in this environment."""
    return _BOTO3_AVAILABLE


# --- Time / TTL helpers ---------------------------------------------------------

def _to_epoch_seconds(dt: datetime) -> int:
    """Return ``dt`` as integer epoch seconds, treating naive values as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def compute_ttl(session: Session, timeout_min: int = DEFAULT_TIMEOUT_MIN) -> int:
    """Compute the DynamoDB TTL (epoch seconds) for ``session`` (Req 4.3).

    The item should expire ``timeout_min`` minutes after the session's last
    activity, matching the inactivity timeout used by
    :func:`src.domain.session.is_active`. When ``last_activity`` is not set the
    TTL is computed from the current UTC time so the item never lingers forever.
    """
    base = session.last_activity or datetime.now(timezone.utc)
    return _to_epoch_seconds(base) + timeout_min * 60


# --- Serialization --------------------------------------------------------------

def _serialize_message(m: Message) -> dict[str, Any]:
    return {"role": m.role, "text": m.text, "at": m.at.isoformat()}


def _deserialize_message(raw: dict[str, Any]) -> Message:
    return Message(
        role=raw["role"],
        text=raw["text"],
        at=datetime.fromisoformat(raw["at"]),
    )


def serialize_session(
    session: Session, timeout_min: int = DEFAULT_TIMEOUT_MIN
) -> dict[str, Any]:
    """Convert a :class:`Session` into a DynamoDB item dict with a TTL (Req 4.3).

    Datetimes are stored as ISO-8601 strings; the ``ttl`` attribute holds epoch
    seconds so DynamoDB can expire the record automatically.
    """
    item: dict[str, Any] = {
        "session_id": session.session_id,
        "user_key": session.user_key,
        "channel": session.channel,
        "messages": [_serialize_message(m) for m in session.messages],
        "active": session.active,
        "ttl": compute_ttl(session, timeout_min),
    }
    if session.last_activity is not None:
        item["last_activity"] = session.last_activity.isoformat()
    return item


def deserialize_session(item: dict[str, Any]) -> Session:
    """Rebuild a :class:`Session` from a DynamoDB item produced by this adapter."""
    last_activity_raw = item.get("last_activity")
    return Session(
        session_id=item["session_id"],
        user_key=item["user_key"],
        channel=item["channel"],
        messages=tuple(
            _deserialize_message(m) for m in item.get("messages", [])
        ),
        last_activity=(
            datetime.fromisoformat(last_activity_raw)
            if last_activity_raw is not None
            else None
        ),
        active=bool(item.get("active", True)),
    )


# --- Credentials via Secrets Manager (Req 10.1) ---------------------------------

@dataclass(frozen=True)
class AwsCredentials:
    """AWS credentials loaded from Secrets Manager. Never logged or hardcoded."""

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None


def load_aws_credentials(
    secret_id: str,
    *,
    secrets_client: Any = None,
    region_name: str | None = None,
) -> AwsCredentials:
    """Load AWS credentials from a Secrets Manager secret (Req 10.1).

    The secret is expected to be a JSON document with ``aws_access_key_id`` and
    ``aws_secret_access_key`` (and optionally ``aws_session_token``). No secret
    value is ever hardcoded; ``secret_id`` names the secret to retrieve. A
    Secrets Manager client may be injected for testing; otherwise one is created
    via boto3.

    Raises :class:`SessionStoreError` if the secret cannot be read or parsed.
    """
    client = secrets_client
    if client is None:
        if not _BOTO3_AVAILABLE:
            raise SessionStoreError(
                "boto3 is not available; inject a secrets_client to load credentials"
            )
        client = boto3.client("secretsmanager", region_name=region_name)

    try:
        response = client.get_secret_value(SecretId=secret_id)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - AWS error path
        raise SessionStoreError(
            f"failed to read credentials from Secrets Manager: {exc}"
        ) from exc

    secret_string = response.get("SecretString")
    if not secret_string:
        raise SessionStoreError(
            f"secret {secret_id!r} has no SecretString payload"
        )

    try:
        data = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise SessionStoreError(
            f"secret {secret_id!r} is not valid JSON"
        ) from exc

    try:
        return AwsCredentials(
            access_key_id=data["aws_access_key_id"],
            secret_access_key=data["aws_secret_access_key"],
            session_token=data.get("aws_session_token"),
        )
    except KeyError as exc:
        raise SessionStoreError(
            f"secret {secret_id!r} is missing required credential field {exc}"
        ) from exc


# --- DynamoDB adapter -----------------------------------------------------------

class SessionStore:
    """DynamoDB-backed persistence for the Gestor_de_Sessao (Req 4.1, 4.3, 10.1).

    Instances wrap a DynamoDB ``Table`` resource. The resource is injected so the
    adapter can be exercised with mocks (``moto`` or fakes) without touching real
    AWS. Use :meth:`from_secrets_manager` to build an instance whose underlying
    boto3 session authenticates with credentials read from Secrets Manager.
    """

    def __init__(self, table: Any, *, timeout_min: int = DEFAULT_TIMEOUT_MIN):
        self._table = table
        self._timeout_min = timeout_min

    @classmethod
    def from_secrets_manager(
        cls,
        table_name: str,
        credentials_secret_id: str,
        *,
        region_name: str | None = None,
        secrets_client: Any = None,
        timeout_min: int = DEFAULT_TIMEOUT_MIN,
    ) -> "SessionStore":
        """Build a store using credentials read from Secrets Manager (Req 10.1).

        No secret is embedded in the code: ``credentials_secret_id`` identifies
        the Secrets Manager secret that holds the AWS credentials. Requires
        boto3 to be installed.
        """
        if not _BOTO3_AVAILABLE:
            raise SessionStoreError("boto3 is required to build a live SessionStore")

        creds = load_aws_credentials(
            credentials_secret_id,
            secrets_client=secrets_client,
            region_name=region_name,
        )
        session = boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            aws_session_token=creds.session_token,
            region_name=region_name,
        )
        resource = session.resource("dynamodb", config=Config(retries={"max_attempts": 3}))
        return cls(resource.Table(table_name), timeout_min=timeout_min)

    # -- writes ------------------------------------------------------------------

    def put_session(self, session: Session) -> None:
        """Persist ``session`` (with TTL) into DynamoDB (Req 4.1, 4.3)."""
        item = serialize_session(session, self._timeout_min)
        try:
            self._table.put_item(Item=item)
        except (BotoCoreError, ClientError) as exc:  # pragma: no cover - AWS error path
            raise SessionStoreError(f"failed to persist session: {exc}") from exc

    def expire_and_persist(self, session: Session) -> Session:
        """Expire ``session`` (clear history, mark inactive) and persist it.

        Combines the deterministic cleanup of
        :func:`src.domain.session.expire_session` with the DynamoDB TTL so the
        history is removed both immediately and by the service (Req 4.3).
        """
        expired = expire_session(session)
        self.put_session(expired)
        return expired

    def delete_session(self, session_id: str) -> None:
        """Delete a session item by ``session_id``."""
        try:
            self._table.delete_item(Key={"session_id": session_id})
        except (BotoCoreError, ClientError) as exc:  # pragma: no cover - AWS error path
            raise SessionStoreError(f"failed to delete session: {exc}") from exc

    # -- reads -------------------------------------------------------------------

    def get_session(self, session_id: str) -> Session | None:
        """Return the session with ``session_id`` or ``None`` if absent."""
        try:
            response = self._table.get_item(Key={"session_id": session_id})
        except (BotoCoreError, ClientError) as exc:  # pragma: no cover - AWS error path
            raise SessionStoreError(f"failed to read session: {exc}") from exc

        item = response.get("Item")
        return deserialize_session(item) if item else None

    def list_sessions_for_user(self, user_key: str, channel: str) -> list[Session]:
        """Return all stored sessions for ``(user_key, channel)`` via the GSI."""
        try:
            from boto3.dynamodb.conditions import Key  # local import: optional dep

            response = self._table.query(
                IndexName=USER_CHANNEL_INDEX,
                KeyConditionExpression=Key("user_key").eq(user_key)
                & Key("channel").eq(channel),
            )
        except (BotoCoreError, ClientError) as exc:  # pragma: no cover - AWS error path
            raise SessionStoreError(f"failed to query sessions: {exc}") from exc

        return [deserialize_session(item) for item in response.get("Items", [])]

    def resolve_active_session(
        self,
        user_key: str,
        channel: str,
        now: datetime,
        timeout_min: int | None = None,
    ) -> Session | None:
        """Return the active session for the pair, or ``None`` (Req 4.4, 4.5).

        Loads candidate sessions from DynamoDB and delegates the active/inactive
        decision to the pure-logic :func:`src.domain.session.resolve_session`,
        so the persistence layer and the domain agree on what "active" means. A
        ``None`` result signals that a new session must be created before
        processing (Req 4.5).
        """
        effective_timeout = self._timeout_min if timeout_min is None else timeout_min
        candidates = self.list_sessions_for_user(user_key, channel)
        return resolve_session(candidates, user_key, channel, now, effective_timeout)
