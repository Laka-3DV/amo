"""Zero-token regressions for wrapper-owned follow-up queries."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    CancelQueuedQuery,
    CommandAck,
    Error,
    GetQueuedQuery,
    Hello,
    Query,
    QueuedQueryDetail,
    QueryQueueState,
    QueuedQueryInfo,
    QueuedQueryUpdated,
    UpdateQueuedQuery,
    deserialize,
    is_downstream,
    serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def _deferred(
    msg_id: str,
    *,
    delivery: str = "queue",
    cmd_id: str | None = None,
) -> Query:
    return Query(
        sid="session-queue",
        prompt=msg_id,
        msg_id=msg_id,
        delivery=delivery,
        cmd_id=cmd_id or f"command-{msg_id}",
        client_id="client-queue",
    )


def test_protocol_v25_deferred_query_and_projection_roundtrip():
    queued = _deferred("queued-1")
    assert deserialize(serialize(queued)) == queued
    with pytest.raises(ValidationError):
        Query(
            sid="session-queue",
            prompt="missing reliable identity",
            msg_id="queued-invalid",
            delivery="queue",
        )

    cancel = CancelQueuedQuery(
        sid="session-queue",
        msg_id="queued-1",
        cmd_id="cancel-1",
        client_id="client-queue",
    )
    assert deserialize(serialize(cancel)) == cancel

    detail_request = GetQueuedQuery(
        sid="session-queue",
        msg_id="queued-1",
        cmd_id="detail-1",
        client_id="client-queue",
    )
    assert deserialize(serialize(detail_request)) == detail_request

    detail = QueuedQueryDetail(
        sid="session-queue",
        msg_id="queued-1",
        request_id="detail-1",
        prompt="full follow up",
        kind="queue",
        images=[{"media_type": "image/png", "data": "c2FtcGxlLWltYWdl"}],
        files=[{"filename": "note.txt", "data": "c2FtcGxlLWZpbGU="}],
        image_count=1,
        file_count=0,
        to="client-queue",
    )
    assert deserialize(serialize(detail)) == detail
    assert is_downstream(detail) is False

    update = UpdateQueuedQuery(
        sid="session-queue",
        msg_id="queued-1",
        prompt="edited follow up",
        cmd_id="update-1",
        client_id="client-queue",
    )
    assert deserialize(serialize(update)) == update

    updated = QueuedQueryUpdated(
        sid="session-queue",
        msg_id="queued-1",
        request_id="update-1",
        updated=True,
        to="client-queue",
    )
    assert deserialize(serialize(updated)) == updated
    assert is_downstream(updated) is False

    state = QueryQueueState(
        sid="session-queue",
        items=[
            QueuedQueryInfo(
                msg_id="queued-1",
                kind="queue",
                prompt_preview="follow up",
                image_count=1,
                file_count=0,
                retained_bytes=2048,
            )
        ],
        total_count=1,
        total_bytes=2048,
    )
    assert deserialize(serialize(state)) == state
    assert is_downstream(state) is True
