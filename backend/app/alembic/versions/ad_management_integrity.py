"""Add integrity fences to the advertising management contracts.

The original ``ad_management`` revision is already deployed in some
environments.  Keep that revision immutable and put the stronger generation,
BC, preview and attempt fences in a follow-up revision.
"""

import sqlalchemy as sa
from alembic import op

revision = "ad_management_integrity"
down_revision = "ad_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A capability is valid only for the exact binding and authorization
    # generations from which its evidence was produced.
    op.create_unique_constraint(
        "uq_bc_binding_authorization_revision",
        "bc_connection_binding",
        [
            "tenant_id",
            "bc_id",
            "connection_id",
            "authorization_revision",
            "revision",
        ],
    )
    op.create_foreign_key(
        "fk_management_capability_binding_revision",
        "management_capability",
        "bc_connection_binding",
        [
            "tenant_id",
            "bc_id",
            "connection_id",
            "authorization_revision",
            "binding_revision",
        ],
        ["tenant_id", "bc_id", "connection_id", "authorization_revision", "revision"],
    )
    op.create_foreign_key(
        "fk_management_capability_authorization_revision",
        "management_capability",
        "connection_authorization",
        ["tenant_id", "connection_id", "authorization_revision"],
        ["tenant_id", "connection_id", "authorization_revision"],
    )
    op.drop_constraint(
        "ck_management_capability_operation", "management_capability", type_="check"
    )
    op.create_check_constraint(
        "ck_management_capability_operation",
        "management_capability",
        "operation IN ('update_roas','update_budget','set_status','set_material_status')",
    )

    # Include BC in the frozen selection reference.  A tenant's selection from
    # one BC must never be attachable to a preview for another BC.
    op.create_unique_constraint(
        "uq_frozen_selection_scope_id",
        "frozen_selection",
        ["tenant_id", "bc_id", "id"],
    )
    op.drop_constraint(
        "fk_management_preview_selection", "management_preview", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_management_preview_selection",
        "management_preview",
        "frozen_selection",
        ["tenant_id", "bc_id", "selection_id"],
        ["tenant_id", "bc_id", "id"],
    )

    # Composite references below make the preview identity part of every task
    # item relationship, rather than trusting application-level matching.
    op.create_unique_constraint(
        "uq_management_preview_item_scope",
        "management_preview_item",
        ["tenant_id", "preview_id", "id"],
    )
    op.create_unique_constraint(
        "uq_management_task_preview_scope",
        "management_task",
        ["tenant_id", "id", "preview_id"],
    )
    op.add_column(
        "management_task_item",
        sa.Column("preview_id", sa.Uuid(), nullable=True),
    )
    # Existing rows from the original revision can be populated from their
    # already-enforced task reference before the column becomes mandatory.
    op.execute(
        sa.text(
            "UPDATE management_task_item AS item "
            "SET preview_id = task.preview_id "
            "FROM management_task AS task "
            "WHERE task.tenant_id = item.tenant_id AND task.id = item.task_id"
        )
    )
    op.alter_column("management_task_item", "preview_id", nullable=False)
    op.create_foreign_key(
        "fk_management_task_item_task_preview",
        "management_task_item",
        "management_task",
        ["tenant_id", "task_id", "preview_id"],
        ["tenant_id", "id", "preview_id"],
    )
    op.create_foreign_key(
        "fk_management_task_item_preview_scope",
        "management_task_item",
        "management_preview_item",
        ["tenant_id", "preview_id", "preview_item_id"],
        ["tenant_id", "preview_id", "id"],
    )

    op.create_check_constraint(
        "ck_management_request_attempt_number",
        "management_request_attempt",
        "attempt >= 1",
    )
    op.create_unique_constraint(
        "uq_management_request_attempt_number",
        "management_request_attempt",
        ["tenant_id", "task_item_id", "attempt"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_management_request_attempt_number",
        "management_request_attempt",
        type_="unique",
    )
    op.drop_constraint(
        "ck_management_request_attempt_number",
        "management_request_attempt",
        type_="check",
    )
    op.drop_constraint(
        "fk_management_task_item_preview_scope",
        "management_task_item",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_management_task_item_task_preview",
        "management_task_item",
        type_="foreignkey",
    )
    op.drop_column("management_task_item", "preview_id")
    op.drop_constraint(
        "uq_management_task_preview_scope", "management_task", type_="unique"
    )
    op.drop_constraint(
        "uq_management_preview_item_scope", "management_preview_item", type_="unique"
    )

    op.drop_constraint(
        "fk_management_preview_selection", "management_preview", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_management_preview_selection",
        "management_preview",
        "frozen_selection",
        ["tenant_id", "selection_id"],
        ["tenant_id", "id"],
    )
    op.drop_constraint(
        "uq_frozen_selection_scope_id", "frozen_selection", type_="unique"
    )

    op.drop_constraint(
        "ck_management_capability_operation", "management_capability", type_="check"
    )
    op.create_check_constraint(
        "ck_management_capability_operation",
        "management_capability",
        "operation IN ('update_roas','update_budget','set_status','set_material_status','ads_manage')",
    )
    op.drop_constraint(
        "fk_management_capability_authorization_revision",
        "management_capability",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_management_capability_binding_revision",
        "management_capability",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_bc_binding_authorization_revision",
        "bc_connection_binding",
        type_="unique",
    )
