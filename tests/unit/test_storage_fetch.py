from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aerospike_native import ResultCode
from agent_squad.types import ConversationMessage, ParticipantRole

from agent_squad_aerospike import AerospikeChatStorage
from agent_squad_aerospike._codec import encode_message
from agent_squad_aerospike._keys import conversation_key
from agent_squad_aerospike.config import AerospikeConfig


def _make_key(value: str) -> object:
    config = AerospikeConfig()
    return SimpleNamespace(
        namespace=config.namespace,
        set_name=config.conversation_set,
        value=value,
    )


class Stream:
    def __init__(self, rows: list[object], include_missing: bool = False) -> None:
        if include_missing:
            self.rows = rows
        else:
            self.rows = [
                row
                for row in rows
                if getattr(row, "result_code", None) != ResultCode.KEY_NOT_FOUND_ERROR
            ]

    async def first(self) -> object | None:
        return self.rows[0] if self.rows else None

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def rows():  # type: ignore[no-untyped-def]
            for row in self.rows:
                yield row

        return rows()


class Query:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.include_missing = False

    def include_missing_keys(self) -> "Query":
        self.include_missing = True
        return self

    async def execute(self, *args: object, **kwargs: object) -> "Stream":
        return Stream(self.rows, include_missing=self.include_missing)


def row(
    bins: dict[str, object] | None = None,
    *,
    code: object = ResultCode.OK,
    key_value: str | None = None,
) -> object:
    record = None if bins is None else SimpleNamespace(bins=bins)
    return SimpleNamespace(
        key=_make_key(key_value) if key_value is not None else None,
        result_code=code,
        record=record,
        exception=None,
        record_or_raise=lambda: record,
    )


def entry(role: ParticipantRole, text: str, timestamp: int) -> dict[str, object]:
    message = ConversationMessage(role, [{"text": text}])
    return {
        "role": role.value,
        "timestamp": timestamp,
        "payload": encode_message(message, role=role.value, timestamp=timestamp),
    }


async def test_fetch_chat_handles_missing_and_applies_read_only_even_limit() -> None:
    session = Mock()
    session.query.side_effect = [
        Query([row(code=ResultCode.KEY_NOT_FOUND_ERROR)]),
        Query([row({"msgs": [entry(ParticipantRole.USER, str(i), i) for i in range(6)]})]),
    ]
    storage = AerospikeChatStorage(session)
    assert await storage.fetch_chat("u", "s", "a") == []
    messages = await storage.fetch_chat("u", "s", "a", max_history_size=3)
    assert [message.content for message in messages] == [[{"text": "4"}], [{"text": "5"}]]


async def test_fetch_all_chats_batches_orders_attributes_and_tolerates_missing() -> None:
    directory = row({"agents": {"b": 2, "a": 1, "missing": 3}})
    conversations = [
        row(
            {"msgs": [entry(ParticipantRole.ASSISTANT, "from b", 10)]},
            key_value=conversation_key("u", "s", "b"),
        ),
        row(
            {"msgs": [entry(ParticipantRole.USER, "from a", 10)]},
            key_value=conversation_key("u", "s", "a"),
        ),
        row(
            code=ResultCode.KEY_NOT_FOUND_ERROR,
            key_value=conversation_key("u", "s", "missing"),
        ),
    ]
    session = Mock()
    session.query.side_effect = [Query([directory]), Query(conversations).include_missing_keys()]
    storage = AerospikeChatStorage(session)
    messages = await storage.fetch_all_chats("u", "s")
    assert [message.content for message in messages] == [
        [{"text": "from a"}],
        [{"text": "[b] from b"}],
    ]
    assert session.query.call_count == 2
    assert len(session.query.call_args.args) == 3


async def test_fetch_all_chats_missing_record_does_not_mislabel_remaining_agents() -> None:
    directory = row({"agents": {"a": 1, "missing": 2, "b": 3}})
    conversations = [
        row(
            {"msgs": [entry(ParticipantRole.USER, "from a", 10)]},
            key_value=conversation_key("u", "s", "a"),
        ),
        row(
            code=ResultCode.KEY_NOT_FOUND_ERROR,
            key_value=conversation_key("u", "s", "missing"),
        ),
        row(
            {"msgs": [entry(ParticipantRole.ASSISTANT, "from b", 20)]},
            key_value=conversation_key("u", "s", "b"),
        ),
    ]
    session = Mock()
    session.query.side_effect = [Query([directory]), Query(conversations).include_missing_keys()]
    storage = AerospikeChatStorage(session)
    messages = await storage.fetch_all_chats("u", "s")
    assert [message.content for message in messages] == [
        [{"text": "from a"}],
        [{"text": "[b] from b"}],
    ]


async def test_fetch_all_chats_ignores_stream_order() -> None:
    directory = row({"agents": {"a": 1, "b": 2, "c": 3}})
    conversations = [
        row(
            {"msgs": [entry(ParticipantRole.ASSISTANT, "from c", 30)]},
            key_value=conversation_key("u", "s", "c"),
        ),
        row(
            code=ResultCode.KEY_NOT_FOUND_ERROR,
            key_value=conversation_key("u", "s", "b"),
        ),
        row(
            {"msgs": [entry(ParticipantRole.USER, "from a", 10)]},
            key_value=conversation_key("u", "s", "a"),
        ),
    ]
    session = Mock()
    session.query.side_effect = [Query([directory]), Query(conversations).include_missing_keys()]
    storage = AerospikeChatStorage(session)
    messages = await storage.fetch_all_chats("u", "s")
    assert [message.content for message in messages] == [
        [{"text": "from a"}],
        [{"text": "[c] from c"}],
    ]


async def test_fetch_all_chats_surfaces_per_key_failures() -> None:
    failed = row(code=ResultCode.TIMEOUT, key_value=conversation_key("u", "s", "a"))
    failed.exception = RuntimeError("batch failed")
    session = Mock()
    session.query.side_effect = [
        Query([row({"agents": {"a": 1}})]),
        Query([failed]).include_missing_keys(),
    ]
    storage = AerospikeChatStorage(session)
    with pytest.raises(RuntimeError, match="batch failed"):
        await storage.fetch_all_chats("u", "s")
