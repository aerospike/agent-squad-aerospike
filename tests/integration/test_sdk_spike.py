from uuid import uuid4

import pytest
from aerospike_sdk import ClusterDefinition, DataSet

from agent_squad_aerospike._sdk import atomic_append


@pytest.mark.integration
async def test_atomic_append_uses_one_public_sdk_write_segment() -> None:
    key = DataSet.of("test", "as_spike").id(uuid4().hex)
    async with ClusterDefinition("localhost", 3000).connect() as client:
        session = client.create_session()

        assert await atomic_append(
            session,
            key,
            [{"role": "user"}, {"role": "assistant"}],
            first_role="user",
            limit=4,
            updated=100,
            ttl_seconds=None,
        )
        assert await atomic_append(
            session,
            key,
            [{"role": "user"}, {"role": "assistant"}, {"role": "user"}],
            first_role="user",
            limit=4,
            updated=200,
            ttl_seconds=60,
        )

        stream = await session.query(key).execute()
        result = await stream.first_or_raise()
        assert result.record_or_raise().bins == {
            "msgs": [
                {"role": "assistant"},
                {"role": "user"},
                {"role": "assistant"},
                {"role": "user"},
            ],
            "updated": 200,
            "schema": 1,
        }

        assert not await atomic_append(
            session,
            key,
            [{"role": "user"}],
            first_role="user",
            limit=4,
            updated=300,
            ttl_seconds=None,
        )
