import base64
import hashlib
import hmac
import json
from collections import defaultdict
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.sql.selectable import Exists
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.accounts.schemas import InputLine, ResolvedLine
from app.modules.tenants.permissions import require_tenant


def parse_matching_rows(
    lines: list[InputLine], rows: list[dict[str, str]]
) -> list[ResolvedLine]:
    by_id = {row["advertiser_id"]: row for row in rows}
    by_name: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_name[row["name"]].add(row["advertiser_id"])
    seen: dict[str, int] = {}
    results = []
    for line in lines:
        text = line.raw.strip()
        candidates = [text] if text in by_id else sorted(by_name.get(text, set()))
        result = ResolvedLine(
            line_no=line.line_no,
            raw=line.raw,
            status="NOT_FOUND",
            candidates=candidates,
        )
        if not text:
            result.status = "EMPTY"
        elif len(candidates) > 1:
            result.status = "AMBIGUOUS"
        elif len(candidates) == 1:
            account_id = candidates[0]
            result.advertiser_id = account_id
            result.status = "DUPLICATE" if account_id in seen else "MATCHED"
            result.duplicate_of = seen.get(account_id)
            seen.setdefault(account_id, line.line_no)
        results.append(result)
    return results


def bc_directory_scope(tenant_id: UUID, bc_id: str) -> Exists:
    """EXISTS preserves one directory row across multiple connection grants."""
    return (
        select(BCAccountAccess.advertiser_id)
        .where(
            BCAccountAccess.tenant_id == tenant_id,
            BCAccountAccess.bc_id == bc_id,
            BCAccountAccess.advertiser_id == AdvertiserAccount.advertiser_id,
        )
        .exists()
    )


def resolve_lines(
    session: Session, *, context: TenantContext, bc_id: str, lines: list[InputLine]
) -> list[ResolvedLine]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if not 1 <= len(lines) <= 500 or len({line.line_no for line in lines}) != len(
        lines
    ):
        raise DomainError("invalid_resolve_request", "每次请求需1至500行，行号不能重复")
    texts = sorted({line.raw.strip() for line in lines if line.raw.strip()})
    rows: dict[str, dict[str, str]] = {}
    for offset in range(0, len(texts), 500):
        chunk = texts[offset : offset + 500]
        statement = select(AdvertiserAccount).where(
            AdvertiserAccount.tenant_id == context.tenant_id,
            bc_directory_scope(context.tenant_id, bc_id),
            or_(
                col(AdvertiserAccount.advertiser_id).in_(chunk),
                col(AdvertiserAccount.name).in_(chunk),
            ),
        )
        for account in session.exec(
            statement.execution_options(populate_existing=True)
        ):
            rows[account.advertiser_id] = {
                "advertiser_id": account.advertiser_id,
                "name": account.name,
            }
    result = parse_matching_rows(lines, list(rows.values()))
    missing = {line.raw.strip() for line in result if line.status == "NOT_FOUND"}
    # Exact ID diagnosis is tenant-local. No query ever probes another tenant.
    elsewhere = (
        set(
            session.exec(
                select(AdvertiserAccount.advertiser_id).where(
                    AdvertiserAccount.tenant_id == context.tenant_id,
                    col(AdvertiserAccount.advertiser_id).in_(missing),
                )
            ).all()
        )
        if missing
        else set()
    )
    for line in result:
        if line.status == "NOT_FOUND" and line.raw.strip() in elsewhere:
            line.status, line.reason = "BLOCKED", "account_not_in_bc"
            line.advertiser_id = line.raw.strip()
        if line.status == "MATCHED":
            assert line.advertiser_id is not None
            try:
                resolve_account_access(
                    session,
                    context=context,
                    bc_id=bc_id,
                    advertiser_id=line.advertiser_id,
                    action="build",
                )
            except DomainError as error:
                line.status, line.reason = "BLOCKED", error.code
    return result


def encode_cursor(*, scope: Mapping[str, str | None], last_id: str) -> str:
    payload = json.dumps(
        {"scope": dict(scope), "last_id": last_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature + payload).decode()


def decode_cursor(cursor: str | None, *, scope: Mapping[str, str | None]) -> str | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 4096:
            raise ValueError
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        signature, payload = raw[:32], raw[32:]
        if not hmac.compare_digest(
            signature,
            hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).digest(),
        ):
            raise ValueError
        value = json.loads(payload)
        if (
            set(value) != {"scope", "last_id"}
            or value["scope"] != scope
            or not isinstance(value["last_id"], str)
            or not value["last_id"]
            or len(value["last_id"]) > 128
        ):
            raise ValueError
        return value["last_id"]
    except ValueError, TypeError, KeyError, UnicodeError:
        raise DomainError("invalid_cursor", "分页游标无效或不属于当前查询") from None
