"""Unit tests for the DynamoDB session-store adapter (Task 12.2).

AWS is mocked with ``moto`` so no real AWS calls are made. Tests cover
serialization/TTL, persistence round-trips, active-session resolution via the
GSI, deterministic expiry, and credential loading from Secrets Manager
(Req 4.1, 4.3, 10.1).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from src.adapters import session_store as store_mod
from src.adapters.session_store import (
    USER_CHANNEL_INDEX,
    AwsCredentials,
    SessionStore,
    SessionStoreError,
    compute_ttl,
    deserialize_session,
    load_aws_credentials,
    serialize_session,
)
from src.domain.config import DEFAULT_TIMEOUT_MIN
from src.domain.models import Message, Session

REGION = "us-east-1"
TABLE_NAME = "sessions"


def _make_session(**overrides) -> Session:
    now = overrides.pop("now", datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    defaults = dict(
        session_id="s-1",
        user_key="user-42",
        channel="site",
        messages=(
            Message(role="user", text="olá", at=now),
            Message(role="assistant", text="oi", at=now + timedelta(seconds=1)),
        ),
        last_activity=now + timedelta(seconds=1),
        active=True,
    )
    defaults.update(overrides)
    return Session(**defaults)


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name=REGION)
        table = client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "user_key", "AttributeType": "S"},
                {"AttributeName": "channel", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": USER_CHANNEL_INDEX,
                    "KeySchema": [
                        {"AttributeName": "user_key", "KeyType": "HASH"},
                        {"AttributeName": "channel", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table


# --- serialization / TTL --------------------------------------------------------

def test_serialize_includes_ttl_after_last_activity():
    session = _make_session()
    item = serialize_session(session, timeout_min=30)

    expected_ttl = int(session.last_activity.timestamp()) + 30 * 60
    assert item["ttl"] == expected_ttl
    assert item["session_id"] == "s-1"
    assert item["messages"][0] == {
        "role": "user",
        "text": "olá",
        "at": session.messages[0].at.isoformat(),
    }


def test_compute_ttl_treats_naive_datetime_as_utc():
    naive = datetime(2025, 1, 1, 12, 0, 0)  # no tzinfo
    session = _make_session(last_activity=naive, messages=())
    aware = naive.replace(tzinfo=timezone.utc)
    assert compute_ttl(session, 30) == int(aware.timestamp()) + 30 * 60


def test_serialize_deserialize_round_trip():
    session = _make_session()
    restored = deserialize_session(serialize_session(session))
    assert restored == session


# --- persistence ----------------------------------------------------------------

def test_put_and_get_session_round_trip(dynamodb_table):
    store = SessionStore(dynamodb_table)
    session = _make_session()

    store.put_session(session)
    loaded = store.get_session(session.session_id)

    assert loaded == session


def test_get_missing_session_returns_none(dynamodb_table):
    store = SessionStore(dynamodb_table)
    assert store.get_session("does-not-exist") is None


def test_put_writes_ttl_attribute(dynamodb_table):
    store = SessionStore(dynamodb_table, timeout_min=DEFAULT_TIMEOUT_MIN)
    session = _make_session()
    store.put_session(session)

    raw = dynamodb_table.get_item(Key={"session_id": session.session_id})["Item"]
    assert int(raw["ttl"]) == compute_ttl(session, DEFAULT_TIMEOUT_MIN)


def test_delete_session(dynamodb_table):
    store = SessionStore(dynamodb_table)
    session = _make_session()
    store.put_session(session)

    store.delete_session(session.session_id)
    assert store.get_session(session.session_id) is None


# --- active session resolution (Req 4.4, 4.5) -----------------------------------

def test_resolve_active_session_returns_matching_active(dynamodb_table):
    store = SessionStore(dynamodb_table)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    session = _make_session(last_activity=now, messages=())
    store.put_session(session)

    resolved = store.resolve_active_session("user-42", "site", now, timeout_min=30)
    assert resolved == session


def test_resolve_returns_none_when_expired(dynamodb_table):
    store = SessionStore(dynamodb_table)
    old = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    session = _make_session(last_activity=old, messages=())
    store.put_session(session)

    later = old + timedelta(minutes=31)
    assert store.resolve_active_session("user-42", "site", later, timeout_min=30) is None


def test_resolve_returns_none_for_unknown_user(dynamodb_table):
    store = SessionStore(dynamodb_table)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    store.put_session(_make_session(last_activity=now, messages=()))

    assert store.resolve_active_session("other", "site", now, timeout_min=30) is None


def test_resolve_distinguishes_channel(dynamodb_table):
    store = SessionStore(dynamodb_table)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    store.put_session(
        _make_session(session_id="s-site", channel="site", last_activity=now, messages=())
    )
    store.put_session(
        _make_session(
            session_id="s-wa", channel="whatsapp", last_activity=now, messages=()
        )
    )

    wa = store.resolve_active_session("user-42", "whatsapp", now, timeout_min=30)
    assert wa is not None and wa.session_id == "s-wa"


# --- deterministic expiry (Req 4.3) ---------------------------------------------

def test_expire_and_persist_clears_history(dynamodb_table):
    store = SessionStore(dynamodb_table)
    session = _make_session()
    store.put_session(session)

    expired = store.expire_and_persist(session)
    assert expired.active is False
    assert expired.messages == ()

    reloaded = store.get_session(session.session_id)
    assert reloaded.active is False
    assert reloaded.messages == ()


# --- credentials via Secrets Manager (Req 10.1) ---------------------------------

def test_load_aws_credentials_from_secrets_manager():
    with mock_aws():
        client = boto3.client("secretsmanager", region_name=REGION)
        client.create_secret(
            Name="progep/dynamodb-creds",
            SecretString=json.dumps(
                {
                    "aws_access_key_id": "AKIAEXAMPLE",
                    "aws_secret_access_key": "secret-value",
                    "aws_session_token": "token-value",
                }
            ),
        )

        creds = load_aws_credentials(
            "progep/dynamodb-creds", secrets_client=client, region_name=REGION
        )

    assert creds == AwsCredentials(
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret-value",
        session_token="token-value",
    )


def test_load_aws_credentials_missing_field_raises():
    with mock_aws():
        client = boto3.client("secretsmanager", region_name=REGION)
        client.create_secret(
            Name="incomplete",
            SecretString=json.dumps({"aws_access_key_id": "only-id"}),
        )
        with pytest.raises(SessionStoreError):
            load_aws_credentials("incomplete", secrets_client=client)


def test_load_aws_credentials_invalid_json_raises():
    with mock_aws():
        client = boto3.client("secretsmanager", region_name=REGION)
        client.create_secret(Name="bad", SecretString="not-json")
        with pytest.raises(SessionStoreError):
            load_aws_credentials("bad", secrets_client=client)


def test_from_secrets_manager_builds_store_without_hardcoded_secrets():
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name=REGION)
        sm.create_secret(
            Name="progep/creds",
            SecretString=json.dumps(
                {
                    "aws_access_key_id": "AKIAEXAMPLE",
                    "aws_secret_access_key": "secret-value",
                }
            ),
        )
        # Pre-create the table so the store operates against it.
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()

        store = SessionStore.from_secrets_manager(
            TABLE_NAME,
            "progep/creds",
            region_name=REGION,
            secrets_client=sm,
        )
        session = _make_session()
        store.put_session(session)
        assert store.get_session(session.session_id) == session


# --- import guard ---------------------------------------------------------------

def test_boto3_available_reports_true_here():
    # boto3 is installed in the test environment.
    assert store_mod.boto3_available() is True
