import os
import socket
from uuid import uuid4

import boto3
import pytest

LOCALSTACK_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", AWS_REGION)
os.environ.setdefault("AWS_REGION", AWS_REGION)
os.environ.setdefault("AWS_ENDPOINT_URL", LOCALSTACK_ENDPOINT)
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def aws_test_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ENDPOINT_URL", LOCALSTACK_ENDPOINT)


@pytest.fixture
def localstack_available(aws_test_env):
    host, port = _host_port_from_endpoint(LOCALSTACK_ENDPOINT)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        pytest.skip(f"LocalStack is not available at {LOCALSTACK_ENDPOINT}")


@pytest.fixture
def local_sqs_client(localstack_available):
    return boto3.client("sqs", region_name=AWS_REGION, endpoint_url=LOCALSTACK_ENDPOINT)


@pytest.fixture
def unique_queue_name():
    def build(prefix):
        return f"{prefix}-{uuid4().hex}"

    return build


def _host_port_from_endpoint(endpoint):
    without_scheme = endpoint.removeprefix("http://").removeprefix("https://")
    host_port = without_scheme.split("/", 1)[0]
    if ":" not in host_port:
        return host_port, 443 if endpoint.startswith("https://") else 80
    host, port = host_port.rsplit(":", 1)
    return host, int(port)
