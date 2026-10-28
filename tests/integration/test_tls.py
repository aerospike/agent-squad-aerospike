"""Opt-in TLS integration tests.

Run against the Enterprise Edition fixture:
    sh docker/gen-tls-certs.sh
    docker compose --profile tls up -d aerospike-tls
    sh docker/provision-pki-user.sh
    AEROSPIKE_TLS_SEED=127.0.0.1:aerospike-tls:4433 \
    AEROSPIKE_TLS_CA=docker/tls-certs/ca.crt \
    pytest -m tls

Tested fixture image (override with AEROSPIKE_TLS_IMAGE for other EE releases):
    aerospike/aerospike-server-enterprise@sha256:ac93616c66bda965098b65a4138df585987fb27daf5b77b8882119f7904a9a60
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from aerospike_sdk import AerospikeError
from agent_squad.types import ConversationMessage, ParticipantRole

from agent_squad_aerospike import AerospikeChatStorage, AerospikeConfig, TlsSettings

TESTED_IMAGE = (
    "aerospike/aerospike-server-enterprise"
    "@sha256:ac93616c66bda965098b65a4138df585987fb27daf5b77b8882119f7904a9a60"
)
TLS_NAME = "aerospike-tls"

_CERT_DIR = Path(__file__).resolve().parents[2] / "docker" / "tls-certs"

SEED = os.environ.get("AEROSPIKE_TLS_SEED")
CA_FILE = os.environ.get("AEROSPIKE_TLS_CA")
CLIENT_CERT = os.environ.get("AEROSPIKE_TLS_CLIENT_CERT", str(_CERT_DIR / "client.crt"))
CLIENT_KEY = os.environ.get("AEROSPIKE_TLS_CLIENT_KEY", str(_CERT_DIR / "client.key"))
UNTRUSTED_CA = os.environ.get("AEROSPIKE_TLS_UNTRUSTED_CA", str(_CERT_DIR / "other-ca.crt"))
UNTRUSTED_CLIENT_CERT = os.environ.get(
    "AEROSPIKE_TLS_UNTRUSTED_CLIENT_CERT", str(_CERT_DIR / "untrusted-client.crt")
)
UNTRUSTED_CLIENT_KEY = os.environ.get(
    "AEROSPIKE_TLS_UNTRUSTED_CLIENT_KEY", str(_CERT_DIR / "untrusted-client.key")
)
OTHER_USER_CERT = os.environ.get("AEROSPIKE_TLS_OTHER_USER_CERT", str(_CERT_DIR / "other-user.crt"))
OTHER_USER_KEY = os.environ.get("AEROSPIKE_TLS_OTHER_USER_KEY", str(_CERT_DIR / "other-user.key"))
MISMATCH_KEY = os.environ.get("AEROSPIKE_TLS_MISMATCH_KEY", str(_CERT_DIR / "mismatch.key"))

pytestmark = [
    pytest.mark.tls,
    pytest.mark.skipif(
        not (SEED and CA_FILE),
        reason="TLS fixture not enabled: set AEROSPIKE_TLS_SEED and AEROSPIKE_TLS_CA",
    ),
]


def _seed(tls_name: str = TLS_NAME) -> str:
    host, _, port = (SEED or "").rpartition(":")
    if ":" in host:
        host = host.rpartition(":")[0]
    return f"{host}:{tls_name}:{port}"


def _config(**overrides: object) -> AerospikeConfig:
    kwargs = {
        "seeds": (_seed(),),
        "tls": TlsSettings(
            ca_file=CA_FILE,
            client_cert_file=CLIENT_CERT,
            client_key_file=CLIENT_KEY,
        ),
        "auth_mode": "certificate",
    }
    kwargs.update(overrides)
    return AerospikeConfig(**kwargs)  # type: ignore[arg-type]


async def _round_trip(config: AerospikeConfig) -> None:
    session_id = uuid4().hex
    async with await AerospikeChatStorage.connect(config) as storage:
        saved = await storage.save_chat_message(
            "user",
            session_id,
            "agent",
            ConversationMessage(ParticipantRole.USER, [{"text": "hello tls"}]),
        )
        assert saved
        fetched = await storage.fetch_chat("user", session_id, "agent")
        assert len(fetched) == 1


async def test_tls_connect_write_read() -> None:
    await _round_trip(_config())


async def test_wrong_ca_fails_closed() -> None:
    config = _config(
        tls=TlsSettings(
            ca_file=UNTRUSTED_CA,
            client_cert_file=CLIENT_CERT,
            client_key_file=CLIENT_KEY,
        )
    )
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


async def test_wrong_tls_name_fails_even_with_matching_san() -> None:
    config = _config(seeds=(_seed(tls_name="not-the-right-name"),))
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


async def test_tls_seed_against_plaintext_port_fails() -> None:
    host = _seed().rsplit(":", 2)[0]
    config = _config(seeds=(f"{host}:{TLS_NAME}:3000",))
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


async def test_missing_client_cert_fails_under_mtls() -> None:
    config = AerospikeConfig(seeds=(_seed(),), tls=TlsSettings(ca_file=CA_FILE))
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


async def test_untrusted_client_cert_fails_under_mtls() -> None:
    config = _config(
        tls=TlsSettings(
            ca_file=CA_FILE,
            client_cert_file=UNTRUSTED_CLIENT_CERT,
            client_key_file=UNTRUSTED_CLIENT_KEY,
        )
    )
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


async def test_unprovisioned_pki_user_fails() -> None:
    config = _config(
        tls=TlsSettings(
            ca_file=CA_FILE,
            client_cert_file=OTHER_USER_CERT,
            client_key_file=OTHER_USER_KEY,
        )
    )
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


async def test_unreadable_ca_material_fails_at_connect() -> None:
    config = _config(
        tls=TlsSettings(
            ca_file=str(_CERT_DIR / "does-not-exist.pem"),
            client_cert_file=CLIENT_CERT,
            client_key_file=CLIENT_KEY,
        )
    )
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


async def test_mismatched_client_cert_key_fails() -> None:
    config = _config(
        tls=TlsSettings(
            ca_file=CA_FILE,
            client_cert_file=CLIENT_CERT,
            client_key_file=MISMATCH_KEY,
        )
    )
    with pytest.raises(AerospikeError):
        await AerospikeChatStorage.connect(config)


@pytest.mark.parametrize(
    "field",
    ["protocols", "ciphers"],
)
async def test_unsupported_allowlist_values_raise_at_connect(field: str) -> None:
    tls = TlsSettings(
        ca_file=CA_FILE,
        client_cert_file=CLIENT_CERT,
        client_key_file=CLIENT_KEY,
        **{field: ("bogus-value",)},
    )
    with pytest.raises(ValueError):
        await AerospikeChatStorage.connect(_config(tls=tls))


async def test_for_login_only_data_plane_is_cleartext() -> None:
    config = AerospikeConfig(
        seeds=(_seed(),),
        username="admin",
        password="admin",
        tls=TlsSettings(
            ca_file=CA_FILE,
            client_cert_file=CLIENT_CERT,
            client_key_file=CLIENT_KEY,
            for_login_only=True,
        ),
    )
    with pytest.raises(AerospikeError, match="for_login_only|non-TLS|cleartext"):
        await AerospikeChatStorage.connect(config)
