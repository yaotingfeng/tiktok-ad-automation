"""Monotonic directory fences; empty scopes retain a revision tombstone."""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKeyConstraint, text
from sqlmodel import Field, SQLModel


class DirectoryRevision(SQLModel, table=True):
    __tablename__ = "account_directory_revision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "bc_id"],
            ["tenant_bc.tenant_id", "tenant_bc.bc_id"],
            name="fk_directory_revision_bc",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["tiktok_connection.tenant_id", "tiktok_connection.id"],
            name="fk_directory_revision_connection",
            ondelete="CASCADE",
        ),
        CheckConstraint("revision >= 0", name="ck_directory_revision_nonnegative"),
    )
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    connection_id: UUID = Field(primary_key=True)
    revision: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
