import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "docker" / "gen-tls-certs.sh"
PROVISIONER = ROOT / "docker" / "provision-pki-user.sh"


def generate(directory: Path) -> subprocess.CompletedProcess[str]:
    script = directory.parent / "gen-tls-certs.sh"
    shutil.copy2(GENERATOR, script)
    return subprocess.run(
        ["sh", str(script)],
        env={**os.environ, "AEROSPIKE_TLS_CERT_DIR": str(directory)},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def material(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("openssl") is None:
        pytest.skip("certificate script tests require OpenSSL 3")
    version = subprocess.run(["openssl", "version"], capture_output=True, text=True, check=True)
    if not version.stdout.startswith("OpenSSL 3."):
        pytest.skip("certificate script tests require OpenSSL 3")
    directory = tmp_path_factory.mktemp("tls") / "certs"
    result = generate(directory)
    assert result.returncode == 0, result.stderr
    assert (directory / "client.crt").is_file()
    return directory


def test_fresh_material_and_reuse(material: Path) -> None:
    before = {p.name: p.read_bytes() for p in material.glob("*.crt")}
    result = generate(material)
    assert result.returncode == 0, result.stderr
    assert before == {p.name: p.read_bytes() for p in material.glob("*.crt")}
    assert (material / ".complete").is_file()
    server = material / "server"
    assert {p.name for p in server.iterdir()} == {"server.crt", "server.key", "ca.crt"}
    for path in material.rglob("*.key"):
        assert path.stat().st_mode & 0o077 == 0
    assert (server / "server.crt").read_bytes() == (material / "server.crt").read_bytes()
    client = subprocess.run(
        ["openssl", "x509", "-in", str(material / "client.crt"), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Version: 3 (0x2)" in client.stdout
    assert "TLS Web Client Authentication" in client.stdout


def test_ca_and_server_extensions(material: Path) -> None:
    for identity in ("ca", "other-ca", "server"):
        result = subprocess.run(
            ["openssl", "x509", "-in", str(material / f"{identity}.crt"), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=True,
        )
        if identity == "server":
            assert "TLS Web Server Authentication" in result.stdout
            assert "Digital Signature, Key Encipherment" in result.stdout
        else:
            assert "X509v3 Basic Constraints: critical" in result.stdout
            assert "CA:TRUE" in result.stdout
            assert "X509v3 Key Usage: critical" in result.stdout
            assert "Certificate Sign" in result.stdout


def test_adopt_legacy_material(material: Path, tmp_path: Path) -> None:
    target = tmp_path / "legacy"
    target.mkdir()
    for path in material.glob("*.*"):
        if path.is_file() and not path.name.startswith("."):
            shutil.copy2(path, target / path.name)
    before = (target / "ca.crt").read_bytes()
    result = generate(target)
    assert result.returncode == 0, result.stderr
    assert (target / "ca.crt").read_bytes() == before
    assert (target / ".complete").is_file()


@pytest.mark.parametrize("damage", ["missing", "key", "trust", "expired", "server-copy"])
def test_invalid_material_is_not_rotated(material: Path, tmp_path: Path, damage: str) -> None:
    target = tmp_path / "certs"
    shutil.copytree(material, target)
    before = (target / "ca.crt").read_bytes()
    if damage == "missing":
        (target / "client.crt").unlink()
    elif damage == "key":
        shutil.copyfile(target / "mismatch.key", target / "client.key")
    elif damage == "trust":
        shutil.copyfile(target / "other-ca.crt", target / "ca.crt")
        before = (target / "ca.crt").read_bytes()
    elif damage == "server-copy":
        shutil.copyfile(target / "client.key", target / "server" / "server.key")
    else:
        subprocess.run(
            [
                "openssl",
                "x509",
                "-in",
                str(target / "client.crt"),
                "-x509toreq",
                "-signkey",
                str(target / "client.key"),
                "-out",
                str(tmp_path / "client.csr"),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(tmp_path / "client.csr"),
                "-CA",
                str(target / "ca.crt"),
                "-CAkey",
                str(target / "ca.key"),
                "-set_serial",
                "42",
                "-days",
                "0",
                "-out",
                str(target / "client.crt"),
            ],
            check=True,
            capture_output=True,
        )
    result = generate(target)
    assert result.returncode != 0
    assert "reset" in result.stderr.lower()
    assert (target / "ca.crt").read_bytes() == before


@pytest.mark.parametrize("state", [".init-lock", ".staging", "unexpected"])
def test_partial_or_locked_state_fails(tmp_path: Path, state: str) -> None:
    target = tmp_path / "certs"
    target.mkdir()
    (target / state).mkdir()
    result = generate(target)
    assert result.returncode != 0
    assert not (target / "ca.crt").exists()
    assert "reset" in result.stderr.lower() or "lock" in result.stderr.lower()


def test_concurrent_and_interrupted_generation(tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    openssl = tools / "openssl"
    openssl.write_text('#!/bin/sh\nsleep 2\necho "simulated generation failure" >&2\nexit 23\n')
    openssl.chmod(0o755)
    script = tmp_path / "generate.sh"
    shutil.copy2(GENERATOR, script)
    target = tmp_path / "certs"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "AEROSPIKE_TLS_CERT_DIR": str(target),
    }
    with subprocess.Popen(
        ["sh", str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as first:
        try:
            deadline = time.monotonic() + 5
            while not (target / ".staging").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (target / ".staging").exists()
            second = subprocess.run(
                ["sh", str(script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert second.returncode != 0
            assert "lock" in second.stderr.lower()
            _, stderr = first.communicate(timeout=10)
            assert first.returncode != 0
            assert "reset" in stderr.lower()
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()
    assert not (target / ".complete").exists()
    retry = generate(target)
    assert retry.returncode != 0
    assert "Interrupted" in retry.stderr


@pytest.fixture
def fake_tools(tmp_path: Path) -> dict[str, str]:
    tools = tmp_path / "bin"
    tools.mkdir()
    timeout = tools / "timeout"
    timeout.write_text('#!/bin/sh\nshift\nexec "$@"\n')
    timeout.chmod(0o755)
    asadm = tools / "asadm"
    asadm.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        '  *"create user"*) touch "$FAKE_CREATED"; echo created; exit 0;;\n'
        "esac\n"
        'if [ "${FAKE_STATE:-}" = network ]; then echo "connection failed"; exit 1; fi\n'
        'echo "${FAKE_HEADER-              User|     Roles|   Auth Mode}"\n'
        'echo "admin|user-admin|password,PKI"\n'
        'if [ "${FAKE_STATE:-}" != absent ] || [ -f "$FAKE_CREATED" ]; then\n'
        '  echo "agent-squad-client|${FAKE_ROLE:-read-write}|${FAKE_AUTH:-PKI}"\n'
        "fi\n"
    )
    asadm.chmod(0o755)
    asinfo = tools / "asinfo"
    asinfo.write_text('#!/bin/sh\n[ "${FAKE_STATE:-}" != tls-fail ] || exit 1\necho ok\n')
    asinfo.chmod(0o755)
    docker = tools / "docker"
    docker.write_text(
        '#!/bin/sh\necho "Docker access forbidden in fixture unit tests" >&2\nexit 97\n'
    )
    docker.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "FAKE_CREATED": str(tmp_path / "created"),
    }


@pytest.mark.parametrize("state", ["correct", "absent"])
def test_provision_correct_or_missing_user(fake_tools: dict[str, str], state: str) -> None:
    result = subprocess.run(
        ["sh", str(PROVISIONER)],
        env={**fake_tools, "FAKE_STATE": state},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout.lower()
    if state == "absent":
        Path(fake_tools["FAKE_CREATED"]).unlink()
    repeated = subprocess.run(
        ["sh", str(PROVISIONER)],
        env={**fake_tools, "FAKE_STATE": state},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert repeated.returncode == 0, repeated.stderr


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Roles|User|Auth Mode",
        "User|Auth Mode|Roles",
        "User|Roles|Auth Mode|Extra",
        "admin|Roles|Auth Mode",
        "User|Roles|Auth Mode\nUser|Roles|Auth Mode",
    ],
)
def test_provision_rejects_unexpected_header(fake_tools: dict[str, str], header: str) -> None:
    result = subprocess.run(
        ["sh", str(PROVISIONER)],
        env={**fake_tools, "FAKE_HEADER": header},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "header" in result.stderr.lower()
    assert "ready" not in result.stdout.lower()
    assert not Path(fake_tools["FAKE_CREATED"]).exists()


def test_stale_lock_recovery_hint(tmp_path: Path) -> None:
    target = tmp_path / "certs"
    lock = target / ".init-lock"
    lock.mkdir(parents=True)
    result = generate(target)
    assert result.returncode != 0
    assert "no initializer is active" in result.stderr
    assert "rmdir" in result.stderr
    assert str(lock) in result.stderr
    assert lock.is_dir()


@pytest.mark.parametrize(
    "overrides",
    [
        {"FAKE_ROLE": "user-admin"},
        {"FAKE_AUTH": "password,PKI"},
        {"FAKE_STATE": "network"},
        {"FAKE_STATE": "tls-fail"},
    ],
)
def test_provision_fails_on_drift_or_connection_failure(
    fake_tools: dict[str, str],
    overrides: dict[str, str],
) -> None:
    result = subprocess.run(
        ["sh", str(PROVISIONER)],
        env={**fake_tools, **overrides},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "ready" not in result.stdout.lower()
