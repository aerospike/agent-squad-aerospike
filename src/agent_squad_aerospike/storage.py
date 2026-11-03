import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Optional, Union

from aerospike_native import ResultCode
from aerospike_sdk import Behavior, ClusterDefinition, DataSet, ErrorStrategy, Host
from aerospike_sdk.policy import Settings
from agent_squad.storage.chat_storage import ChatStorage
from agent_squad.types import ConversationMessage, ParticipantRole, TimestampedMessage

from ._codec import decode_message, encode_message
from ._keys import conversation_key, directory_key
from ._sdk import atomic_append, register_membership
from .config import AerospikeConfig


class AerospikeChatStorage(ChatStorage):
    def __init__(
        self,
        session: Any,
        config: AerospikeConfig | None = None,
        *,
        owned_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._config = config or AerospikeConfig()
        self._owned_client = owned_client
        self._clock = clock
        self._memberships: OrderedDict[tuple[str, str, str], float] = OrderedDict()
        lock_count = min(self._config.membership_cache_capacity, 256)
        self._membership_locks = tuple(asyncio.Lock() for _ in range(lock_count))
        self._closed = False

    @classmethod
    async def connect(
        cls,
        config: AerospikeConfig | None = None,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> "AerospikeChatStorage":
        resolved = config or AerospikeConfig()
        if client_factory is not None:
            if resolved.tls is not None:
                raise ValueError("client_factory cannot satisfy configured TLS settings")
            client = client_factory(",".join(resolved.seeds))
            await client.connect()
        else:
            tls_name = resolved.tls.tls_name if resolved.tls is not None else None
            hosts = []
            for seed in resolved.seeds:
                name, seed_tls_name, port = _parse_seed(seed)
                if seed_tls_name is not None and resolved.tls is None:
                    raise ValueError(
                        f"Seed {seed!r} carries a TLS name but no TLS settings are configured"
                    )
                effective_tls_name = seed_tls_name or tls_name
                if resolved.auth_mode == "certificate" and effective_tls_name is None:
                    raise ValueError(
                        f"auth_mode 'certificate' requires a TLS name for seed {seed!r}"
                    )
                hosts.append(Host(name, port, effective_tls_name))
            definition = ClusterDefinition(hosts=hosts)
            if resolved.tls is not None:
                tls = resolved.tls
                definition.with_tls_config(
                    ca_file=tls.ca_file,
                    tls_name=tls.tls_name,
                    client_cert_file=tls.client_cert_file,
                    client_key_file=tls.client_key_file,
                    protocols=list(tls.protocols) if tls.protocols is not None else None,
                    ciphers=list(tls.ciphers) if tls.ciphers is not None else None,
                    for_login_only=tls.for_login_only,
                )
            if resolved.auth_mode == "certificate":
                definition.with_certificate_credentials()
            elif resolved.username is not None or resolved.password is not None:
                if resolved.username is None or resolved.password is None:
                    raise ValueError("username and password must be configured together")
                definition.with_native_credentials(resolved.username, resolved.password)
            client = await definition.connect()
        behavior = Behavior.DEFAULT.derive_with_changes(
            "agent_squad_aerospike",
            all=Settings(
                total_timeout=timedelta(milliseconds=resolved.total_timeout_ms),
                socket_timeout=timedelta(milliseconds=resolved.socket_timeout_ms),
            ),
            writes=Settings(max_retries=resolved.max_write_retries),
        )
        return cls(client.create_session(behavior), resolved, owned_client=client)

    @property
    def session(self) -> Any:
        return self._session

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owned_client is not None:
            await self._owned_client.close()

    async def __aenter__(self) -> "AerospikeChatStorage":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _ensure_membership(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        updated: int,
    ) -> None:
        membership = (user_id, session_id, agent_id)
        deadline = self._memberships.get(membership)
        now = self._clock()
        if deadline is not None and now < deadline:
            self._memberships.move_to_end(membership)
            return
        lock = self._membership_locks[hash(membership) % len(self._membership_locks)]
        async with lock:
            deadline = self._memberships.get(membership)
            now = self._clock()
            if deadline is not None and now < deadline:
                self._memberships.move_to_end(membership)
                return
            key = DataSet.of(self._config.namespace, self._config.directory_set).id(
                directory_key(user_id, session_id)
            )
            await register_membership(
                self._session,
                key,
                agent_id,
                updated=updated,
                max_agents=self._config.max_agents_per_session,
                ttl_seconds=self._config.ttl_seconds,
            )
            refresh = self._config.membership_refresh_seconds
            self._memberships[membership] = float("inf") if refresh is None else now + refresh
            self._memberships.move_to_end(membership)
            while len(self._memberships) > self._config.membership_cache_capacity:
                self._memberships.popitem(last=False)

    async def save_chat_message(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        new_message: Union[ConversationMessage, TimestampedMessage],
        max_history_size: Optional[int] = None,
    ) -> bool:
        if not user_id or not session_id or not agent_id:
            raise ValueError("user_id, session_id, and agent_id must not be empty")
        role = (
            new_message.role.value
            if isinstance(new_message.role, ParticipantRole)
            else str(new_message.role)
        )
        timestamp = getattr(new_message, "timestamp", None) or int(self._clock() * 1000)
        payload = encode_message(
            new_message,
            role=role,
            timestamp=timestamp,
        )
        entry = {"role": role, "timestamp": timestamp, "payload": payload}
        await self._ensure_membership(user_id, session_id, agent_id, timestamp)
        key = DataSet.of(self._config.namespace, self._config.conversation_set).id(
            conversation_key(user_id, session_id, agent_id)
        )
        return await atomic_append(
            self._session,
            key,
            [entry],
            first_role=role,
            limit=self._config.effective_history_limit(max_history_size),
            updated=timestamp,
            ttl_seconds=self._config.ttl_seconds,
        )

    async def save_chat_messages(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        new_messages: Union[list[ConversationMessage], list[TimestampedMessage]],
        max_history_size: Optional[int] = None,
    ) -> bool:
        if not new_messages:
            return False
        roles = [
            message.role.value if isinstance(message.role, ParticipantRole) else str(message.role)
            for message in new_messages
        ]
        if any(left == right for left, right in zip(roles, roles[1:], strict=False)):
            raise ValueError("message roles must alternate")
        entries: list[dict[str, Any]] = []
        for message, role in zip(new_messages, roles, strict=True):
            timestamp = int(getattr(message, "timestamp", None) or self._clock() * 1000)
            entries.append(
                {
                    "role": role,
                    "timestamp": timestamp,
                    "payload": encode_message(
                        message,
                        role=role,
                        timestamp=timestamp,
                    ),
                }
            )
        await self._ensure_membership(
            user_id,
            session_id,
            agent_id,
            entries[-1]["timestamp"],
        )
        key = DataSet.of(self._config.namespace, self._config.conversation_set).id(
            conversation_key(user_id, session_id, agent_id)
        )
        saved = await atomic_append(
            self._session,
            key,
            entries,
            first_role=roles[0],
            limit=self._config.effective_history_limit(max_history_size),
            updated=entries[-1]["timestamp"],
            ttl_seconds=self._config.ttl_seconds,
        )
        if not saved:
            raise ValueError("first message role matches persisted last role")
        return True

    async def fetch_chat(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        max_history_size: int | None = None,
    ) -> list[Any]:
        key = DataSet.of(self._config.namespace, self._config.conversation_set).id(
            conversation_key(user_id, session_id, agent_id)
        )
        stream = await self._session.query(key).execute(on_error=ErrorStrategy.IN_STREAM)
        result = await stream.first()
        if result is None or result.result_code == ResultCode.KEY_NOT_FOUND_ERROR:
            return []
        if result.result_code != ResultCode.OK:
            result.record_or_raise()
        entries = result.record.bins.get("msgs", [])
        limit = self._config.effective_history_limit(max_history_size)
        return [decode_message(entry["payload"]) for entry in entries[-limit:]]

    async def fetch_all_chats(self, user_id: str, session_id: str) -> list[Any]:
        directory = DataSet.of(self._config.namespace, self._config.directory_set).id(
            directory_key(user_id, session_id)
        )
        directory_stream = await self._session.query(directory).execute(
            on_error=ErrorStrategy.IN_STREAM
        )
        directory_result = await directory_stream.first()
        if (
            directory_result is None
            or directory_result.result_code == ResultCode.KEY_NOT_FOUND_ERROR
        ):
            return []
        if directory_result.result_code != ResultCode.OK:
            directory_result.record_or_raise()
        agents = list(dict.fromkeys(directory_result.record.bins.get("agents", {})))
        if not agents:
            return []
        keys: list[Any] = []
        key_to_agent: dict[tuple[str, str, str], str] = {}
        for agent_id in agents:
            key = DataSet.of(self._config.namespace, self._config.conversation_set).id(
                conversation_key(user_id, session_id, agent_id)
            )
            keys.append(key)
            key_to_agent[(key.namespace, key.set_name, key.value)] = agent_id
        stream = await (
            self._session.query(*keys)
            .include_missing_keys()
            .execute(on_error=ErrorStrategy.IN_STREAM)
        )
        merged: list[tuple[int, str, int, Any]] = []
        index = 0
        async for agent_id, result in _zip_results(key_to_agent, stream):
            if result.result_code == ResultCode.KEY_NOT_FOUND_ERROR:
                continue
            if result.result_code != ResultCode.OK:
                if result.exception is not None:
                    raise result.exception
                result.record_or_raise()
            for entry in result.record.bins.get("msgs", []):
                message = decode_message(entry["payload"])
                if message.role == ParticipantRole.ASSISTANT:
                    content = message.content
                    if content and isinstance(content[0], dict) and "text" in content[0]:
                        content[0]["text"] = f"[{agent_id}] {content[0]['text']}"
                    message.content = content
                merged.append((entry["timestamp"], agent_id, index, message))
                index += 1
        merged.sort(key=lambda item: item[:3])
        return [item[3] for item in merged]


def _parse_seed(seed: str) -> tuple[str, str | None, int]:
    if seed.startswith("["):
        host, separator, rest = seed[1:].partition("]:")
        if not separator or not host:
            raise ValueError(f"Invalid Aerospike seed {seed!r}; expected host:port")
        segments = rest.split(":")
    else:
        if "]" in seed or "[" in seed:
            raise ValueError(f"Invalid Aerospike seed {seed!r}; expected host:port")
        parts = seed.split(":")
        if len(parts) > 3:
            host, separator, rest = seed.rpartition(":")
            segments = [rest] if separator else []
        else:
            host = parts[0]
            segments = parts[1:]
    if not host:
        raise ValueError(f"Invalid Aerospike seed {seed!r}; expected host:port")
    if len(segments) == 1:
        tls_name, port_text = None, segments[0]
    elif len(segments) == 2:
        tls_name, port_text = segments
        if not tls_name:
            raise ValueError(f"Invalid Aerospike seed {seed!r}; TLS name must not be empty")
    else:
        raise ValueError(f"Invalid Aerospike seed {seed!r}; expected host:port")
    try:
        return host, tls_name, int(port_text)
    except ValueError as exc:
        raise ValueError(f"Invalid Aerospike seed port in {seed!r}") from exc


async def _zip_results(key_to_agent: dict[tuple[str, str, str], str], stream: Any) -> Any:
    async for result in stream:
        key = result.key
        agent_id = key_to_agent.get((key.namespace, key.set_name, key.value))
        if agent_id is None:
            raise RuntimeError(f"Aerospike returned unexpected batch key {key!r}")
        yield agent_id, result
