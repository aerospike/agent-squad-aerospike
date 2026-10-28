from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_squad_aerospike import AerospikeChatStorage, AerospikeConfig, TlsSettings
from agent_squad_aerospike.storage import _parse_seed


def _mock_definition() -> Mock:
    client = Mock()
    client.create_session.return_value = object()
    builder = Mock()
    for method in (
        "with_native_credentials",
        "with_certificate_credentials",
        "with_tls_config",
    ):
        getattr(builder, method).return_value = builder
    builder.connect = AsyncMock(return_value=client)
    return builder


async def _connect(config: AerospikeConfig, builder: Mock) -> None:
    with (
        patch(
            "agent_squad_aerospike.storage.ClusterDefinition", return_value=builder
        ) as mock_cluster,
        patch("agent_squad_aerospike.storage.Behavior") as mock_behavior,
    ):
        mock_behavior.DEFAULT = Mock()
        mock_behavior.DEFAULT.derive_with_changes.return_value = "behavior"
        await AerospikeChatStorage.connect(config)
        mock_cluster.assert_called_once()


class TestTlsSettings:
    def test_minimal_config_is_valid(self) -> None:
        tls = TlsSettings()
        assert tls.ca_file is None
        assert tls.for_login_only is False

    def test_full_config_is_valid(self) -> None:
        tls = TlsSettings(
            ca_file="/certs/ca.pem",
            tls_name="prod-tls",
            client_cert_file="/certs/client.pem",
            client_key_file="/certs/client.key",
            protocols=("TLSv1.3",),
            ciphers=("TLS_AES_256_GCM_SHA384",),
            for_login_only=False,
        )
        assert tls.protocols == ("TLSv1.3",)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("client_cert_file", "/certs/client.pem"),
            ("client_key_file", "/certs/client.key"),
        ],
    )
    def test_unpaired_client_credentials_rejected(self, field: str, value: str) -> None:
        with pytest.raises(ValueError, match="together"):
            TlsSettings(**{field: value})

    @pytest.mark.parametrize("field", ["protocols", "ciphers"])
    def test_empty_allowlists_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            TlsSettings(**{field: ()})

    def test_empty_tls_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="tls_name"):
            TlsSettings(tls_name="")


class TestAerospikeConfigTls:
    def test_for_login_only_requires_credentials(self) -> None:
        with pytest.raises(ValueError, match="for_login_only"):
            AerospikeConfig(tls=TlsSettings(for_login_only=True))
        config = AerospikeConfig(username="u", password="p", tls=TlsSettings(for_login_only=True))
        assert config.tls is not None

    def test_certificate_mode_forbids_password(self) -> None:
        tls = TlsSettings(
            tls_name="t",
            client_cert_file="c.pem",
            client_key_file="c.key",
        )
        with pytest.raises(ValueError, match="certificate"):
            AerospikeConfig(username="u", password="p", tls=tls, auth_mode="certificate")

    def test_certificate_mode_requires_mtls(self) -> None:
        with pytest.raises(ValueError, match="certificate"):
            AerospikeConfig(auth_mode="certificate")
        with pytest.raises(ValueError, match="certificate"):
            AerospikeConfig(tls=TlsSettings(tls_name="t"), auth_mode="certificate")

    def test_certificate_mode_valid_config(self) -> None:
        config = AerospikeConfig(
            seeds=("node:my-tls:4333",),
            tls=TlsSettings(
                tls_name="my-tls",
                client_cert_file="c.pem",
                client_key_file="c.key",
            ),
            auth_mode="certificate",
        )
        assert config.auth_mode == "certificate"

    def test_invalid_auth_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="auth_mode"):
            AerospikeConfig(auth_mode="bogus")  # type: ignore[arg-type]


class TestParseSeed:
    @pytest.mark.parametrize(
        ("seed", "expected"),
        [
            ("host:3000", ("host", None, 3000)),
            ("host:my-tls:4333", ("host", "my-tls", 4333)),
            ("127.0.0.1:3000", ("127.0.0.1", None, 3000)),
            ("[::1]:3000", ("::1", None, 3000)),
            ("[::1]:my-tls:4333", ("::1", "my-tls", 4333)),
            ("::1:3000", ("::1", None, 3000)),
            ("host:a:b:3000", ("host:a:b", None, 3000)),
        ],
    )
    def test_valid_forms(self, seed: str, expected: tuple[str, str | None, int]) -> None:
        assert _parse_seed(seed) == expected

    @pytest.mark.parametrize(
        "seed",
        [
            "",
            "host",
            ":3000",
            "host:abc",
            "host::3000",
            "host:3000:extra:abc",
            "[::1]",
            "[::1]3000",
            "[::1]:abc",
            "[::1]:a:b:3000",
            "::1]:3000",
            "[::1:3000",
        ],
    )
    def test_malformed_seeds_rejected(self, seed: str) -> None:
        with pytest.raises(ValueError):
            _parse_seed(seed)


class TestConnectTls:
    async def test_tls_invokes_with_tls_config_with_mapped_values(self) -> None:
        builder = _mock_definition()
        config = AerospikeConfig(
            seeds=("node:4333",),
            tls=TlsSettings(
                ca_file="/certs/ca.pem",
                tls_name="prod-tls",
                client_cert_file="/certs/client.pem",
                client_key_file="/certs/client.key",
                protocols=("TLSv1.3",),
                ciphers=("CIPHER_A",),
            ),
        )
        await _connect(config, builder)
        builder.with_tls_config.assert_called_once_with(
            ca_file="/certs/ca.pem",
            tls_name="prod-tls",
            client_cert_file="/certs/client.pem",
            client_key_file="/certs/client.key",
            protocols=["TLSv1.3"],
            ciphers=["CIPHER_A"],
            for_login_only=False,
        )

    async def test_no_tls_skips_with_tls_config(self) -> None:
        builder = _mock_definition()
        await _connect(AerospikeConfig(), builder)
        builder.with_tls_config.assert_not_called()

    async def test_tls_with_client_factory_rejected(self) -> None:
        factory = Mock()
        with pytest.raises(ValueError, match="client_factory"):
            await AerospikeChatStorage.connect(
                AerospikeConfig(tls=TlsSettings()), client_factory=factory
            )
        factory.assert_not_called()

    async def test_tls_named_seed_without_tls_settings_rejected(self) -> None:
        builder = _mock_definition()
        config = AerospikeConfig(seeds=("node:my-tls:4333",))
        with pytest.raises(ValueError, match="TLS name"):
            await _connect(config, builder)

    async def test_certificate_mode_uses_certificate_credentials(self) -> None:
        builder = _mock_definition()
        config = AerospikeConfig(
            seeds=("node:my-tls:4333",),
            tls=TlsSettings(
                ca_file="/certs/ca.pem",
                client_cert_file="c.pem",
                client_key_file="c.key",
            ),
            auth_mode="certificate",
        )
        await _connect(config, builder)
        builder.with_certificate_credentials.assert_called_once_with()
        builder.with_native_credentials.assert_not_called()

    async def test_certificate_mode_requires_tls_name_on_every_seed(self) -> None:
        builder = _mock_definition()
        tls = TlsSettings(client_cert_file="c.pem", client_key_file="c.key")
        config = AerospikeConfig(seeds=("node:4333",), tls=tls, auth_mode="certificate")
        with pytest.raises(ValueError, match="TLS name"):
            await _connect(config, builder)


class TestTlsNamePrecedence:
    async def test_seed_tls_name_overrides_and_unnamed_inherits(self) -> None:
        builder = _mock_definition()
        config = AerospikeConfig(
            seeds=("node1:prod-tls:4333", "node2:4333"),
            tls=TlsSettings(ca_file="/certs/ca.pem", tls_name="shared-tls"),
        )
        with (
            patch(
                "agent_squad_aerospike.storage.ClusterDefinition", return_value=builder
            ) as mock_cluster,
            patch("agent_squad_aerospike.storage.Behavior") as mock_behavior,
            patch("agent_squad_aerospike.storage.Host") as mock_host,
        ):
            mock_behavior.DEFAULT = Mock()
            await AerospikeChatStorage.connect(config)
        assert mock_host.call_args_list[0].args == ("node1", 4333, "prod-tls")
        assert mock_host.call_args_list[1].args == ("node2", 4333, "shared-tls")
        mock_cluster.assert_called_once()
