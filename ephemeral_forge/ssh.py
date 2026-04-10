"""SSH key generation and remote command execution."""

from __future__ import annotations

import io
import logging
import time

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

logger = logging.getLogger(__name__)


def generate_ssh_keypair() -> tuple[str, str]:
    """Generate an Ed25519 SSH key pair.

    Returns (private_key_pem, public_key_openssh).
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_openssh = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return private_pem, public_openssh


def ssh_connect(
    host: str,
    username: str,
    private_key_pem: str,
    timeout: int = 120,
) -> paramiko.SSHClient:
    """Connect via SSH, retrying until timeout."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key_pem))

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.connect(
                hostname=host,
                username=username,
                pkey=key,
                timeout=10,
                banner_timeout=10,
            )
            return client
        except Exception as e:
            last_error = e
            time.sleep(3)

    raise ConnectionError(f"SSH to {host} failed after {timeout}s: {last_error}")


def run_command(
    client: paramiko.SSHClient,
    command: str,
    timeout: int = 300,
) -> tuple[str, str, int]:
    """Run a command over SSH.

    Returns (stdout, stderr, exit_code).
    """
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return stdout.read().decode(), stderr.read().decode(), exit_code
