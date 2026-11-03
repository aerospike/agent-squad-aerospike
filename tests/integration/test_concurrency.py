import asyncio
from uuid import uuid4

import pytest
from aerospike_sdk import ClusterDefinition
from agent_squad.types import ConversationMessage, ParticipantRole

from agent_squad_aerospike import AerospikeChatStorage


@pytest.mark.integration
async def test_concurrent_same_role_filter_is_atomic_without_lost_accepts() -> None:
    session_id = uuid4().hex
    async with ClusterDefinition("localhost", 3000).connect() as client:
        storage = AerospikeChatStorage(client.create_session())
        assert await storage.save_chat_message(
            "user", session_id, "agent", ConversationMessage(ParticipantRole.USER, [])
        )
        results = await asyncio.gather(
            *(
                storage.save_chat_message(
                    "user",
                    session_id,
                    "agent",
                    ConversationMessage(ParticipantRole.ASSISTANT, [{"text": str(index)}]),
                )
                for index in range(20)
            )
        )
        assert results.count(True) == 1
        assert results.count(False) == 19
