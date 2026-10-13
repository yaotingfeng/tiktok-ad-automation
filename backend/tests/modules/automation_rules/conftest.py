# conftest.py
from datetime import timedelta

import pytest

from app.core.security import create_access_token
from tests.modules.conftest import create_context


def headers(context):
    token = create_access_token(context.actor_id, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_context(session):
    return create_context(session, role="tenant_admin")


@pytest.fixture
def viewer_context(session):
    return create_context(session, role="viewer")
