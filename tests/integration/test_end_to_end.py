import asyncio
from uuid import uuid4

import pytest
from aerospike_sdk import ClusterDefinition
from agent_squad.types import ConversationMessage, ParticipantRole

from agent_squad_aerospike import AerospikeChatStorage, AerospikeConfig, DirectoryFullError


@pytest.mark.integration
async def test_self_created_storage_persists_across_connections() -> None:
    session_id = uuid4().hex
    message = ConversationMessage(ParticipantRole.USER, [{"text": "persisted"}])
    async with await AerospikeChatStorage.connect() as writer:
        assert await writer.save_chat_message("u", session_id, "alpha", message)

    async with await AerospikeChatStorage.connect() as reader:
        history = await reader.fetch_chat("u", session_id, "alpha")
        all_history = await reader.fetch_all_chats("u", session_id)

    assert [item.content for item in history] == [[{"text": "persisted"}]]
    assert [item.content for item in all_history] == [[{"text": "persisted"}]]


@pytest.mark.integration
async def test_rich_message_end_to_end() -> None:
    session_id = uuid4().hex
    async with ClusterDefinition("localhost", 3000).connect() as client:
        storage = AerospikeChatStorage(
            client.create_session(),
            AerospikeConfig(hard_history_limit=4),
        )
        user = ConversationMessage(
            ParticipantRole.USER,
            [{"text": "héllo"}, {"tool": {"bytes": b"\x00\xff"}}],
            citations=[{"source": "doc"}],
        )
        assistant = ConversationMessage(ParticipantRole.ASSISTANT, [{"text": "answer"}])
        assert await storage.save_chat_messages("u", session_id, "alpha", [user, assistant])
        assert not await storage.save_chat_message("u", session_id, "alpha", assistant)
        assert await storage.save_chat_messages("u", session_id, "alpha", [user, assistant])
        history = await storage.fetch_chat("u", session_id, "alpha")
        assert len(history) == 4
        assert history[-2].content == user.content
        assert history[-2].citations == user.citations

        assert await storage.save_chat_message(
            "u",
            session_id,
            "beta",
            ConversationMessage(ParticipantRole.USER, [{"text": "other"}]),
        )
        all_messages = await storage.fetch_all_chats("u", session_id)
        assert len(all_messages) == 5
        assert any(
            message.content and message.content[0].get("text") == "[alpha] answer"
            for message in all_messages
        )


@pytest.mark.integration
async def test_history_limit_discards_oldest_messages() -> None:
    session_id = uuid4().hex
    messages = [
        ConversationMessage(
            ParticipantRole.USER if index % 2 == 0 else ParticipantRole.ASSISTANT,
            [{"text": f"message-{index}"}],
        )
        for index in range(6)
    ]
    async with ClusterDefinition("localhost", 3000).connect() as client:
        storage = AerospikeChatStorage(
            client.create_session(),
            AerospikeConfig(hard_history_limit=4),
        )
        assert await storage.save_chat_messages("u", session_id, "alpha", messages)

        history = await storage.fetch_chat("u", session_id, "alpha")

        assert [message.content for message in history] == [
            [{"text": "message-2"}],
            [{"text": "message-3"}],
            [{"text": "message-4"}],
            [{"text": "message-5"}],
        ]


@pytest.mark.integration
async def test_process_restart_and_sliding_ttl() -> None:
    session_id = uuid4().hex
    config = AerospikeConfig(ttl_seconds=2, membership_refresh_seconds=1)
    async with ClusterDefinition("localhost", 3000).connect() as client:
        session = client.create_session()
        first = AerospikeChatStorage(session, config)
        assert await first.save_chat_message(
            "u", session_id, "a", ConversationMessage(ParticipantRole.USER, [])
        )
        await asyncio.sleep(1.1)
        assert await first.save_chat_message(
            "u", session_id, "a", ConversationMessage(ParticipantRole.ASSISTANT, [])
        )
        restarted = AerospikeChatStorage(session, config)
        assert len(await restarted.fetch_chat("u", session_id, "a")) == 2
        await asyncio.sleep(4.0)
        assert await restarted.fetch_chat("u", session_id, "a") == []
        assert await restarted.fetch_all_chats("u", session_id) == []


@pytest.mark.integration
async def test_adversarial_role_value_suppression_still_holds() -> None:
    session_id = uuid4().hex
    adversarial_role = 'user" or "1"=="1'
    first = ConversationMessage(adversarial_role, [{"text": "first"}])
    second = ConversationMessage(adversarial_role, [{"text": "second"}])
    async with ClusterDefinition("localhost", 3000).connect() as client:
        storage = AerospikeChatStorage(
            client.create_session(),
            AerospikeConfig(hard_history_limit=4),
        )
        assert await storage.save_chat_message("u", session_id, "alpha", first)
        assert not await storage.save_chat_message("u", session_id, "alpha", second)


@pytest.mark.integration
async def test_adversarial_agent_id_does_not_crash_or_bypass_membership_guard() -> None:
    session_id = uuid4().hex
    adversarial_agent = 'agent"bad'
    async with ClusterDefinition("localhost", 3000).connect() as client:
        storage = AerospikeChatStorage(
            client.create_session(),
            AerospikeConfig(max_agents_per_session=1),
        )
        assert await storage.save_chat_message(
            "u",
            session_id,
            adversarial_agent,
            ConversationMessage(ParticipantRole.USER, [{"text": "ok"}]),
        )
        # A second, different agent should be rejected as full.
        with pytest.raises(DirectoryFullError):
            await storage.save_chat_message(
                "u",
                session_id,
                "other",
                ConversationMessage(ParticipantRole.USER, [{"text": "too many"}]),
            )
