# conftest.py
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.security import create_access_token
from tests.modules.conftest import create_context


@pytest.fixture(autouse=True)
def connection_encryption_key(monkeypatch):
    # 飞书 Webhook 与签名密钥加密保存；测试自带临时密钥，不依赖本机 .env（发布检查用隔离环境）。
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


def headers(context):
    token = create_access_token(context.actor_id, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_context(session):
    return create_context(session, role="tenant_admin")


@pytest.fixture
def viewer_context(session):
    return create_context(session, role="viewer")


# 跨 BC 账户种子(acc-2 同时属于 bc-a、bc-b)由报表测试共享。
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed  # noqa: E402
