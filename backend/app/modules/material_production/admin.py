"""历史租户受控开户：从标准输入读取私有 JSON，默认仅校验并回滚。"""

import argparse
import json
import sys
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError

from .service import onboard_existing_tenant


class ExistingTenant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    administrator_id: UUID
    expected_name: str
    expected_username: str
    password: SecretStr | None = None
    generate_password: bool = False


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: UUID
    tenants: list[ExistingTenant] = Field(min_length=1, max_length=20)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="提交已核对租户的开户任务")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError
        request = Request.model_validate_json(raw)
        if len({item.tenant_id for item in request.tenants}) != len(request.tenants):
            raise ValueError
        with Session(engine) as session:
            results = []
            for item in request.tenants:
                status = onboard_existing_tenant(
                    session,
                    actor_id=request.actor_id,
                    tenant_id=item.tenant_id,
                    administrator_id=item.administrator_id,
                    expected_name=item.expected_name,
                    expected_username=item.expected_username,
                    password=item.password.get_secret_value()
                    if item.password
                    else None,
                    generate_password=item.generate_password,
                )
                results.append(
                    {"tenant_id": str(item.tenant_id), **status.model_dump()}
                )
            # 校验所有目标后统一提交。预检不留下用户、任务或开户记录。
            if args.apply:
                session.commit()
            else:
                session.rollback()
        print(  # noqa: T201
            json.dumps({"applied": args.apply, "tenants": results}, ensure_ascii=False)
        )
        return 0
    except DomainError as error:
        print(  # noqa: T201
            json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=False)
        )
    except ValidationError, ValueError:
        # Pydantic 的异常包含输入值，不能直接输出含密码的错误正文。
        print('{"error":"invalid_private_input"}')  # noqa: T201
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
