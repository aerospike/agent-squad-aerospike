from typing import get_type_hints

from agent_squad.storage.chat_storage import ChatStorage

from agent_squad_aerospike import AerospikeChatStorage


def test_package_imports() -> None:
    import agent_squad_aerospike

    assert {
        "AerospikeChatStorage",
        "AerospikeConfig",
        "AgentSquadAerospikeError",
        "AmbiguousWriteError",
        "ConversationTooLargeError",
        "DirectoryFullError",
        "TlsSettings",
        "UnknownMessageSchemaError",
    } == set(agent_squad_aerospike.__all__)


def test_save_method_annotations_match_chat_storage() -> None:
    for method_name in ("save_chat_message", "save_chat_messages"):
        expected = get_type_hints(getattr(ChatStorage, method_name))
        actual = get_type_hints(getattr(AerospikeChatStorage, method_name))
        assert (
            actual["new_message" if method_name == "save_chat_message" else "new_messages"]
            == (expected["new_message" if method_name == "save_chat_message" else "new_messages"])
        )
        assert actual["max_history_size"] == expected["max_history_size"]
