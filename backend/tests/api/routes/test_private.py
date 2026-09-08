from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import User


def test_private_template_signup_not_mounted(client: TestClient, db: Session) -> None:
    response = client.post(
        "/api/private/users/",
        json={
            "email": "private@example.com",
            "password": "password123",
            "full_name": "Private",
        },
    )
    assert response.status_code == 404
    assert (
        db.exec(select(User).where(User.email == "private@example.com")).first() is None
    )
