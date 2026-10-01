"""Property-based test for LLM call-failure recording in the Monitor_de_Consumo.

Feature: assistente-progep-ifs, Property 18: Registro de falha de chamada de LLM
Validates: Requirements 7.4

Property 18: For any chamada de LLM que falha antes de concluir, o registro
produzido pelo Monitor_de_Consumo tem status de falha e o session_id correto, e
os registros previamente efetuados permanecem inalterados.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.monitor import CallRecord, CallStatus, record_llm_failure

# A single arbitrary prior CallRecord: any session id, any status value, and an
# optional reason. Prior records are treated as opaque history that must survive
# untouched, so we do not constrain them to valid statuses only.
_call_records = st.builds(
    CallRecord,
    session_id=st.text(),
    status=st.sampled_from([s.value for s in CallStatus]),
    reason=st.one_of(st.none(), st.text()),
)

# An arbitrary tuple of prior records, in chronological order.
_records_tuple = st.lists(_call_records).map(tuple)

# A non-empty session id for the failing call.
_session_ids = st.text(min_size=1)

_reasons = st.one_of(st.none(), st.text())


@pytest.mark.property
@given(prior=_records_tuple, session_id=_session_ids, reason=_reasons)
def test_p18_llm_failure_record(
    prior: tuple[CallRecord, ...], session_id: str, reason: str | None
) -> None:
    """A failure record is appended with FAILED status and the correct
    session_id, while prior records remain unchanged and unmutated."""
    prior_snapshot = tuple(prior)  # capture identity/order before the call

    result = record_llm_failure(prior, session_id, reason=reason)

    # The new tuple is exactly the prior records followed by the failure record.
    failure = CallRecord(
        session_id=session_id, status=CallStatus.FAILED.value, reason=reason
    )
    assert result == prior + (failure,)

    # The appended record carries the failure status and the correct session_id.
    appended = result[-1]
    assert appended.status == CallStatus.FAILED.value
    assert appended.session_id == session_id

    # Prior records are preserved: same objects, same order.
    assert len(result) == len(prior) + 1
    for original, produced in zip(prior_snapshot, result):
        assert produced is original

    # The input tuple itself is not mutated.
    assert prior == prior_snapshot
    assert len(prior) == len(prior_snapshot)
