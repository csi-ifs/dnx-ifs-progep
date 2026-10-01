"""Integration tests for the AWS I/O adapters (Task 12.8).

Real AWS is not available in this environment, so these integration-style tests
exercise the adapters end-to-end against **mocked AWS**:

* ``moto``'s :func:`moto.mock_aws` is used wherever a real boto3 service is
  supported (S3, DynamoDB, Secrets Manager, CloudWatch, CloudTrail).
* :class:`unittest.mock.MagicMock` / hand-rolled fakes stand in for services
  ``moto`` does not cover (Bedrock agent runtime, ``bedrock-agent`` ingestion,
  Amazon Connect, AWS End User Messaging Social).

Timing/SLA assertions are intentionally lightweight: the tests assert that the
adapters complete and produce the expected structured outputs (they do NOT
assert real wall-clock SLAs).

Coverage map (acceptance criteria):

* Req 1.1 / 1.7 — KB ingestion job triggered + incremental reindex.
* Req 2.1     — KB query returns scored source references (with billing guard).
* Req 3.4     — Connect enqueue independent of attendant availability.
* Req 5.1 / 5.5 — API returns a structured envelope (success + timeout).
* Req 6.1 / 6.2 — WhatsApp receive (via Connect) + deliver (with retry).
* Req 7.1 / 7.2 / 7.3 — CloudWatch metrics emitted for tokens and responses.
* Req 10.6    — IAM ``AccessDenied`` surfaced/handled while CloudTrail audits.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.adapters.agent import BedrockAgentAdapter, BillingGuardError
from src.adapters.api_handler import handler
from src.adapters.connect_router import ConnectRouterAdapter
from src.adapters.knowledge import KnowledgeBaseAdapter
from src.adapters.monitor_adapter import DEFAULT_NAMESPACE, MonitorAdapter
from src.adapters.session_store import SessionStore, load_aws_credentials
from src.adapters.whatsapp_connector import (
    IncomingWhatsAppMessage,
    WhatsAppConnectorAdapter,
)
from src.domain.api import CognitoToken
from src.domain.models import (
    AgentRequest,
    AgentResponse,
    DocumentCandidate,
    Message,
    Session,
)

pytestmark = pytest.mark.integration

REGION = "us-east-1"


@pytest.fixture
def aws_credentials(monkeypatch):
    """Provide dummy AWS credentials so moto never touches real AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def aws(aws_credentials):
    """Activate moto for the duration of a test."""
    with mock_aws():
        yield


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Knowledge_base adapter — ingestion + reindex (Req 1.1, 1.7)
# ---------------------------------------------------------------------------

class TestKnowledgeBaseAdapterIntegration:
    """S3 upload (moto) + Bedrock KB ingestion job (bedrock-agent fake)."""

    def test_upload_and_ingest_triggers_indexing(self, aws):
        # S3 is a real moto-backed service; bedrock-agent ingestion is faked.
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="kb-bucket")

        bedrock_agent = MagicMock()
        bedrock_agent.start_ingestion_job.return_value = {
            "ingestionJob": {"ingestionJobId": "job-1", "status": "STARTING"}
        }

        adapter = KnowledgeBaseAdapter(
            knowledge_base_id="kb-123",
            data_source_id="ds-123",
            bucket="kb-bucket",
            s3_client=s3,
            bedrock_agent_client=bedrock_agent,
        )

        # Upload a document to the data source bucket (real moto S3 round-trip).
        adapter.upload_document(
            "site/edital.pdf", b"%PDF-1.4 conteudo", content_type="application/pdf"
        )
        stored = s3.get_object(Bucket="kb-bucket", Key="site/edital.pdf")
        assert stored["Body"].read() == b"%PDF-1.4 conteudo"

        # Ingesting a valid document triggers the KB ingestion job (Req 1.1).
        doc = DocumentCandidate(
            doc_id="d1",
            source="site",
            filename="edital.pdf",
            content_type="pdf",
            size_bytes=1024,
        )
        record = adapter.ingest_document(doc)

        assert record.status == "indexed"
        assert record.doc_id == "d1"
        assert record.source == "site"
        bedrock_agent.start_ingestion_job.assert_called_once()

    def test_reindex_triggers_incremental_job(self, aws):
        # Reindexation reuses the incremental ingestion job (Req 1.7).
        bedrock_agent = MagicMock()
        bedrock_agent.start_ingestion_job.return_value = {
            "ingestionJob": {"ingestionJobId": "job-2", "status": "STARTING"}
        }
        adapter = KnowledgeBaseAdapter(
            knowledge_base_id="kb-123",
            data_source_id="ds-123",
            bedrock_agent_client=bedrock_agent,
        )

        job = adapter.reindex()

        assert job.started is True
        assert job.job_id == "job-2"
        bedrock_agent.start_ingestion_job.assert_called_once()


# ---------------------------------------------------------------------------
# Agent adapter — KB query returns scored refs + billing guard (Req 2.1, 8.5)
# ---------------------------------------------------------------------------

class TestBedrockAgentAdapterIntegration:
    """Bedrock agent runtime is not covered by moto -> fake kb_client."""

    def test_forward_request_returns_scored_sources(self):
        kb_client = MagicMock()
        kb_client.retrieve.return_value = {
            "retrievalResults": [
                {"location": {"s3Location": {"uri": "s3://kb/doc1.pdf"}}, "score": 0.92},
                {"location": {"s3Location": {"uri": "s3://kb/doc2.pdf"}}, "score": 0.71},
            ]
        }
        adapter = BedrockAgentAdapter("kb-123", kb_client=kb_client)
        request = AgentRequest(session_id="s1", channel="site", text="Qual o prazo?")

        response = adapter.forward_request(request, threshold=0.5)

        assert response.status == "answered"
        assert len(response.sources) == 2
        assert {ref.doc_id for ref in response.sources} == {
            "s3://kb/doc1.pdf",
            "s3://kb/doc2.pdf",
        }
        assert all(ref.score >= 0.5 for ref in response.sources)
        kb_client.retrieve.assert_called_once()

    def test_query_below_threshold_yields_not_found(self):
        kb_client = MagicMock()
        kb_client.retrieve.return_value = {
            "retrievalResults": [
                {"location": {"s3Location": {"uri": "s3://kb/doc1.pdf"}}, "score": 0.10},
            ]
        }
        adapter = BedrockAgentAdapter("kb-123", kb_client=kb_client)
        request = AgentRequest(session_id="s1", channel="site", text="pergunta")

        response = adapter.forward_request(request, threshold=0.5)

        assert response.status == "not_found"
        assert response.sources == ()

    def test_billing_guard_blocks_call_without_user_agent(self):
        # Missing User_Agent_APN -> call blocked before any Bedrock call (Req 8.5).
        kb_client = MagicMock()
        adapter = BedrockAgentAdapter("kb-123", kb_client=kb_client, user_agent_extra=None)
        request = AgentRequest(session_id="s1", channel="site", text="pergunta")

        with pytest.raises(BillingGuardError):
            adapter.forward_request(request, threshold=0.5)
        kb_client.retrieve.assert_not_called()


# ---------------------------------------------------------------------------
# Session store — DynamoDB put/get/resolve + Secrets Manager creds
# (Req 4.1, 4.3, 4.4, 10.1)
# ---------------------------------------------------------------------------

def _create_sessions_table(dynamodb) -> None:
    dynamodb.create_table(
        TableName="sessions",
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "user_key", "AttributeType": "S"},
            {"AttributeName": "channel", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "UserChannelIndex",
                "KeySchema": [
                    {"AttributeName": "user_key", "KeyType": "HASH"},
                    {"AttributeName": "channel", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class TestSessionStoreIntegration:
    """DynamoDB + Secrets Manager are both real moto-backed services."""

    def test_put_get_and_resolve_active_session(self, aws):
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        _create_sessions_table(dynamodb)
        store = SessionStore(dynamodb.Table("sessions"))

        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        session = Session(
            session_id="sess-1",
            user_key="user-1",
            channel="site",
            messages=(Message(role="user", text="ola", at=now),),
            last_activity=now,
            active=True,
        )

        store.put_session(session)

        got = store.get_session("sess-1")
        assert got is not None
        assert got.session_id == "sess-1"
        assert got.user_key == "user-1"
        assert got.messages[0].text == "ola"

        # Resolve the active session for the (user, channel) pair (Req 4.4).
        resolved = store.resolve_active_session(
            "user-1", "site", now + timedelta(minutes=5)
        )
        assert resolved is not None
        assert resolved.session_id == "sess-1"

    def test_expired_session_not_resolved(self, aws):
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        _create_sessions_table(dynamodb)
        store = SessionStore(dynamodb.Table("sessions"))

        old = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        store.put_session(
            Session(
                session_id="sess-old",
                user_key="user-2",
                channel="site",
                messages=(Message(role="user", text="oi", at=old),),
                last_activity=old,
                active=True,
            )
        )

        # Far beyond the inactivity timeout -> no active session (Req 4.3, 4.5).
        resolved = store.resolve_active_session(
            "user-2", "site", old + timedelta(hours=5), timeout_min=30
        )
        assert resolved is None

    def test_load_credentials_from_secrets_manager(self, aws):
        sm = boto3.client("secretsmanager", region_name=REGION)
        sm.create_secret(
            Name="assistente/aws-creds",
            SecretString=json.dumps(
                {
                    "aws_access_key_id": "AKIAEXAMPLE",
                    "aws_secret_access_key": "shhh-secret",
                    "aws_session_token": "tok-123",
                }
            ),
        )

        creds = load_aws_credentials("assistente/aws-creds", secrets_client=sm)

        assert creds.access_key_id == "AKIAEXAMPLE"
        assert creds.secret_access_key == "shhh-secret"
        assert creds.session_token == "tok-123"


# ---------------------------------------------------------------------------
# Connect router — enqueue independent of availability (Req 3.4, 3.3, 3.6)
# ---------------------------------------------------------------------------

class TestConnectRouterAdapterIntegration:
    """Amazon Connect is not covered by moto -> fake connect_client."""

    def test_escalate_enqueues_independent_of_availability(self):
        connect = MagicMock()
        connect.start_chat_contact.return_value = {"ContactId": "contact-1"}
        # No attendant available, yet the contact is still enqueued (Req 3.4).
        connect.get_current_metric_data.return_value = {
            "MetricResults": [
                {
                    "Collections": [
                        {"Metric": {"Name": "CONTACTS_IN_QUEUE"}, "Value": 4.0},
                        {"Metric": {"Name": "OLDEST_CONTACT_AGE"}, "Value": 180.0},
                    ]
                }
            ]
        }

        adapter = ConnectRouterAdapter(
            "instance-1", "flow-1", "queue-1", connect_client=connect
        )
        now = _utcnow()
        session = Session(
            session_id="s1",
            user_key="u1",
            channel="site",
            messages=(
                Message(role="user", text="quero um atendente", at=now),
                Message(role="assistant", text="ok", at=now),
            ),
            last_activity=now,
        )

        result = adapter.escalate(session)

        assert result.escalated is True
        assert result.contact_id == "contact-1"
        assert result.queue_status is not None
        assert result.queue_status.position == 4
        assert result.queue_status.estimated_wait_seconds == 180
        # Full history transferred to Connect (Req 3.3).
        assert len(result.preserved_history) == 2
        connect.start_chat_contact.assert_called_once()

    def test_escalate_unavailable_after_retries_preserves_history(self):
        connect = MagicMock()
        connect.start_chat_contact.side_effect = ConnectionError("connect down")

        adapter = ConnectRouterAdapter(
            "instance-1", "flow-1", "queue-1", connect_client=connect
        )
        now = _utcnow()
        session = Session(
            session_id="s2",
            user_key="u2",
            channel="site",
            messages=(Message(role="user", text="ajuda", at=now),),
            last_activity=now,
        )

        result = adapter.escalate(session)

        assert result.escalated is False
        assert result.unavailable is True
        assert result.attempts_made == 3
        assert result.user_message  # sanitized, non-empty
        assert len(result.preserved_history) == 1  # history preserved (Req 3.5)


# ---------------------------------------------------------------------------
# API handler — structured envelope on success and timeout (Req 5.1, 5.4, 5.5)
# ---------------------------------------------------------------------------

def _valid_verifier():
    verifier = MagicMock()
    verifier.verify.return_value = CognitoToken(
        present=True,
        signature_valid=True,
        expires_at=_utcnow() + timedelta(hours=1),
    )
    return verifier


class TestApiHandlerIntegration:
    """API Gateway/Lambda handler with injected Cognito verifier + agent."""

    def test_success_returns_structured_envelope(self):
        def invoker(request: AgentRequest) -> AgentResponse:
            return AgentResponse(
                status="answered",
                content="Resposta oficial da PROGEP.",
                sources=(),
                confidence=0.95,
            )

        event = {
            "headers": {"Authorization": "Bearer valid-token"},
            "body": json.dumps({"message": "Ola", "sessionId": "sess-1"}),
        }

        result = handler(event, verifier=_valid_verifier(), invoker=invoker)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "ok"
        assert body["content"] == "Resposta oficial da PROGEP."

    def test_unauthenticated_request_is_blocked(self):
        verifier = MagicMock()
        verifier.verify.return_value = CognitoToken(
            present=False, signature_valid=False, expires_at=None
        )
        invoker = MagicMock()
        event = {"headers": {}, "body": json.dumps({"message": "Ola"})}

        result = handler(event, verifier=verifier, invoker=invoker)

        assert result["statusCode"] == 401
        body = json.loads(result["body"])
        assert body["status"] == "error"
        assert body["error"]["code"] == "unauthenticated"
        invoker.assert_not_called()

    def test_agent_timeout_returns_unavailable_envelope(self):
        def slow_invoker(request: AgentRequest) -> AgentResponse:
            time.sleep(0.3)
            return AgentResponse(status="answered", content="late", sources=(), confidence=1.0)

        event = {
            "headers": {"Authorization": "Bearer valid-token"},
            "body": json.dumps({"message": "Ola"}),
        }

        result = handler(
            event,
            verifier=_valid_verifier(),
            invoker=slow_invoker,
            agent_timeout_s=0.05,
        )

        assert result["statusCode"] == 503
        body = json.loads(result["body"])
        assert body["status"] == "error"
        assert body["error"]["code"] == "agent_unavailable"


# ---------------------------------------------------------------------------
# WhatsApp connector — receive/deliver + retry (Req 6.1, 6.2, 6.5)
# ---------------------------------------------------------------------------

class TestWhatsAppConnectorIntegration:
    """Connect + End User Messaging Social are faked (not covered by moto)."""

    def test_receive_text_forwards_via_connect(self):
        connect = MagicMock()
        connect.start_chat_contact.return_value = {"ContactId": "wa-contact-1"}
        social = MagicMock()

        adapter = WhatsAppConnectorAdapter(
            "instance-1",
            "flow-1",
            "phone-1",
            connect_client=connect,
            social_client=social,
            sleeper=lambda _s: None,
        )
        message = IncomingWhatsAppMessage(
            from_number="+5579999990000",
            message_id="m1",
            text="Ola, preciso de ajuda",
            part_types=("text",),
        )

        result = adapter.receive_message(message)

        assert result.forwarded is True
        assert result.contact_id == "wa-contact-1"
        connect.start_chat_contact.assert_called_once()

    def test_receive_discards_non_text_media(self):
        connect = MagicMock()
        social = MagicMock()
        adapter = WhatsAppConnectorAdapter(
            "instance-1",
            "flow-1",
            "phone-1",
            connect_client=connect,
            social_client=social,
            sleeper=lambda _s: None,
        )
        message = IncomingWhatsAppMessage(
            from_number="+5579999990000",
            message_id="m2",
            text="",
            part_types=("image",),
        )

        result = adapter.receive_message(message)

        assert result.forwarded is False
        assert result.warning  # "apenas texto" warning (Req 6.3)
        connect.start_chat_contact.assert_not_called()

    def test_deliver_response_succeeds(self):
        social = MagicMock()
        social.send_whatsapp_message.return_value = {"messageId": "wamid.1"}
        adapter = WhatsAppConnectorAdapter(
            "instance-1",
            "flow-1",
            "phone-1",
            connect_client=MagicMock(),
            social_client=social,
            sleeper=lambda _s: None,
        )

        result = adapter.deliver_response("+5579999990000", "Sua resposta")

        assert result.delivered is True
        assert result.message_id == "wamid.1"
        assert result.attempts_made == 1

    def test_deliver_retries_then_succeeds_with_min_interval(self):
        social = MagicMock()
        social.send_whatsapp_message.side_effect = [
            ConnectionError("transient"),
            {"messageId": "wamid.2"},
        ]
        waits: list[float] = []
        adapter = WhatsAppConnectorAdapter(
            "instance-1",
            "flow-1",
            "phone-1",
            connect_client=MagicMock(),
            social_client=social,
            sleeper=lambda s: waits.append(s),
        )

        result = adapter.deliver_response("+5579999990000", "resposta")

        assert result.delivered is True
        assert result.message_id == "wamid.2"
        assert result.attempts_made == 2
        # Minimum interval of >= 10 s honoured between attempts (Req 6.5).
        assert all(w >= 10 for w in result.waits_s)
        assert waits and all(w >= 10 for w in waits)


# ---------------------------------------------------------------------------
# Monitor adapter — CloudWatch metrics emitted (Req 7.1, 7.2, 7.3, 7.5)
# ---------------------------------------------------------------------------

class TestMonitorAdapterIntegration:
    """CloudWatch is a real moto-backed service."""

    def test_token_usage_and_response_metrics_are_emitted(self, aws):
        cloudwatch = boto3.client("cloudwatch", region_name=REGION)
        adapter = MonitorAdapter(cloudwatch_client=cloudwatch)

        tokens = adapter.record_token_usage("sess-1", input_tokens=120, output_tokens=45)
        response = adapter.record_response("sess-1", "Resposta gerada pelo agente")

        assert tokens.recorded is True
        assert response.recorded is True

        # Metrics are visible in CloudWatch after being published (Req 7.3).
        metrics = cloudwatch.list_metrics(Namespace=DEFAULT_NAMESPACE)["Metrics"]
        names = {m["MetricName"] for m in metrics}
        assert {"InputTokens", "OutputTokens", "Responses", "ResponseLength"} <= names

    def test_record_retries_and_surfaces_session_error(self):
        # Persistent transient failures exhaust the 3-attempt budget (Req 7.5).
        cloudwatch = MagicMock()
        cloudwatch.put_metric_data.side_effect = ConnectionError("cw down")
        adapter = MonitorAdapter(cloudwatch_client=cloudwatch)

        result = adapter.record_token_usage("sess-err", input_tokens=1, output_tokens=1)

        assert result.recorded is False
        assert result.session_id == "sess-err"
        assert result.attempts_made == 3
        assert result.error is not None and "sess-err" in result.error


# ---------------------------------------------------------------------------
# IAM denial surfaced/handled while CloudTrail audits (Req 10.6)
# ---------------------------------------------------------------------------

class TestIamDenialAndCloudTrailIntegration:
    """CloudTrail is real moto-backed; the AccessDenied error is simulated."""

    @staticmethod
    def _trail_bucket_policy(bucket: str) -> str:
        return json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AWSCloudTrailAclCheck",
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudtrail.amazonaws.com"},
                        "Action": "s3:GetBucketAcl",
                        "Resource": f"arn:aws:s3:::{bucket}",
                    },
                    {
                        "Sid": "AWSCloudTrailWrite",
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudtrail.amazonaws.com"},
                        "Action": "s3:PutObject",
                        "Resource": f"arn:aws:s3:::{bucket}/*",
                    },
                ],
            }
        )

    def test_access_denied_is_surfaced_and_cloudtrail_configured(self, aws):
        # 1. An audit trail exists (CloudTrail records auditable events, Req 10.6).
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="audit-trail-bucket")
        s3.put_bucket_policy(
            Bucket="audit-trail-bucket",
            Policy=self._trail_bucket_policy("audit-trail-bucket"),
        )
        cloudtrail = boto3.client("cloudtrail", region_name=REGION)
        cloudtrail.create_trail(
            Name="assistente-audit",
            S3BucketName="audit-trail-bucket",
            IsMultiRegionTrail=False,
        )
        cloudtrail.start_logging(Name="assistente-audit")
        trails = cloudtrail.describe_trails()["trailList"]
        assert any(t["Name"] == "assistente-audit" for t in trails)

        # 2. An operation without sufficient IAM permission is denied; the adapter
        #    handles the AccessDenied error, preserves state (no embeddings) and
        #    records the failure with the doc_id (Req 10.6).
        bedrock_agent = MagicMock()
        bedrock_agent.start_ingestion_job.side_effect = ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "User is not authorized to perform bedrock:StartIngestionJob",
                }
            },
            "StartIngestionJob",
        )
        adapter = KnowledgeBaseAdapter(
            knowledge_base_id="kb-123",
            data_source_id="ds-123",
            bedrock_agent_client=bedrock_agent,
        )
        doc = DocumentCandidate(
            doc_id="d-denied",
            source="site",
            filename="edital.pdf",
            content_type="pdf",
            size_bytes=1024,
        )

        record = adapter.ingest_document(doc)

        assert record.status == "failed"
        assert record.doc_id == "d-denied"
        assert record.reason  # denial reason surfaced, state preserved
