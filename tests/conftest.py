"""Shared pytest fixtures and governance decision logging.

Every test logs a structured decision line containing::

    case=<name> | input=<request inputs> | basis=<why> | status=<final>

so acceptance/rejection reasons are visible in test output (``pytest -s`` or
via ``log_cli``) and distinguishable.
"""

from __future__ import annotations

import itertools
import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.clock import FakeClock
from app.config import build_default_config
from app.main import create_app
from app.signature import sign

# Anchor every test at a fixed timestamp for deterministic windows.
T0 = 2_000_000.0

_logger = logging.getLogger("governance.test")

if not logging.getLogger("governance").handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger("governance")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


def log_decision(case: str, inputs: dict, basis: str, status: object) -> None:
    """Print/log the input, the decision basis and the final status."""
    _logger.info(
        "case=%s | input=%s | basis=%s | status=%s",
        case, json.dumps(inputs, default=str, sort_keys=True), basis, status,
    )


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def config(clock):
    return build_default_config(clock)


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    return TestClient(app)


_nonce = itertools.count(1)


def make_headers(
    clock,
    method: str,
    path: str,
    *,
    caller: str = "acme",
    secret: bytes = b"acme-new-secret",
    body: bytes = b"",
    timestamp: float | None = None,
    nonce: str | None = None,
    signature: str | None = None,
    version: str | int | None = None,
    extra: dict | None = None,
) -> dict[str, str]:
    ts = clock.now() if timestamp is None else timestamp
    ts_str = str(ts)
    nonce = nonce if nonce is not None else f"nonce-{next(_nonce)}"
    sig = signature if signature is not None else sign(
        secret, method, path, ts_str, nonce, body
    )
    headers = {
        "X-Caller-Id": caller,
        "X-Timestamp": ts_str,
        "X-Nonce": nonce,
        "X-Signature": sig,
    }
    if version is not None:
        headers["X-Api-Version"] = str(version)
    if extra:
        headers.update(extra)
    return headers


@pytest.fixture
def sign_headers(clock):
    def _make(*args, **kwargs):
        return make_headers(clock, *args, **kwargs)
    return _make


def post_order(client, headers, payload: dict | bytes):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    return client.request("POST", "/orders", content=payload, headers=headers)
