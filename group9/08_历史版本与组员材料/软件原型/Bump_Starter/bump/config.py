"""Private, per-install settings for the localhost development environment."""

import ipaddress
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME = PROJECT_ROOT / ".runtime"


def _certificates(path: Path) -> None:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bump local development CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
        ]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    (path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (path / "server.pem").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (path / "server.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    # No CA private key is retained. A different runtime gets a different CA.


def init_runtime(path=DEFAULT_RUNTIME, web_port=8765, mqtt_port=18883) -> dict:
    path = Path(path).resolve()
    if (path / "config.json").exists():
        return load_settings(path)
    for port in (web_port, mqtt_port):
        if not 1024 <= port <= 65535:
            raise ValueError("Choose unprivileged ports between 1024 and 65535.")
    if web_port == mqtt_port:
        raise ValueError("HTTP and MQTT need different ports.")
    path.mkdir(parents=True, exist_ok=True)
    _certificates(path)
    users = [
        {"id": name, "password": secrets.token_urlsafe(18),
         "display_name": f"演示用户 {label}", "bio": f"Group 9 测试资料 {label}",
         "contact": f"{name}@example.invalid"}
        for name, label in (("alice", "A"), ("bob", "B"), ("charlie", "C"))
    ]
    config = {
        "config_version": 1,
        "web_port": web_port,
        "mqtt": {
            "host": "localhost", "port": mqtt_port,
            "accounts": {
                name: {"password": secrets.token_urlsafe(24), "client_id": client_id}
                for name, client_id in (
                    ("_worker", "bump-worker"), ("badge-a", "badge-a"), ("badge-b", "badge-b")
                )
            },
        },
        "users": users,
        "devices": {
            "badge-a": {"owner": "alice", "alias": "A00001"},
            "badge-b": {"owner": "bob", "alias": "B00002"},
        },
        "sessions": {"demo-001": {"A00001": "badge-a", "B00002": "badge-b"}},
    }
    (path / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (path / "credentials.txt").write_text(
        "Bump 本机演示账号（自动生成；不要发到群里或提交版本库）\n"
        + "\n".join(f"{u['id']}: {u['password']}" for u in users)
        + "\n\nalice 对应 badge-a；bob 对应 badge-b；charlie 用于第三人权限测试。\n"
          "密码和证书仅用于这一个本地开发环境。全部资料均为虚构。\n",
        encoding="utf-8",
    )
    return load_settings(path)


def load_settings(path=DEFAULT_RUNTIME) -> dict:
    path = Path(path).resolve()
    settings = json.loads((path / "config.json").read_text(encoding="utf-8"))
    if settings.get("config_version") != 1:
        raise ValueError("Unsupported runtime config version.")
    if settings["mqtt"]["host"] != "localhost":
        raise ValueError("This development build only supports localhost.")
    settings["runtime_dir"] = str(path)
    settings["db_path"] = str(path / "bump.sqlite3")
    for key, filename in (
        ("ca_file", "ca.pem"), ("cert_file", "server.pem"), ("key_file", "server.key")
    ):
        settings["mqtt"][key] = str(path / filename)
        if not (path / filename).is_file():
            raise ValueError(f"Missing runtime certificate file: {filename}")
    return settings
