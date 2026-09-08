"""persist shared account application scene preparation

Revision ID: 0008_scene_jobs
Revises: 0007_directory_revision
Create Date: 2026-09-09 07:39:46.388306

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0008_scene_jobs'
down_revision = '0007_directory_revision'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('build_scene_job',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('bc_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('advertiser_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('connection_id', sa.Uuid(), nullable=False),
    sa.Column('credential_version', sa.Integer(), nullable=False),
    sa.Column('provider_connection_id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('minis_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('scope_basis', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('capability_job_id', sa.Uuid(), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('resource', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
    sa.Column('next_page', sa.Integer(), nullable=False),
    sa.Column('facts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('claim_token', sa.Uuid(), nullable=True),
    sa.Column('claimed_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dispatch_id', sa.Uuid(), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('repair_after', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('first_observed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.CheckConstraint("jsonb_typeof(facts) = 'object'", name='ck_build_scene_job_facts'),
    sa.CheckConstraint("resource IN ('capabilities','identity','minis','cta','vbo','regions','done')", name='ck_build_scene_job_resource'),
    sa.CheckConstraint("status IN ('PENDING','COMPLETE','BLOCKED','STALE','FAILED')", name='ck_build_scene_job_status'),
    sa.CheckConstraint('(claim_token IS NULL) = (claimed_until IS NULL)', name='ck_build_scene_job_claim'),
    sa.CheckConstraint('(first_observed_at IS NULL) = (expires_at IS NULL)', name='ck_build_scene_job_observation'),
    sa.CheckConstraint('revision >= 0 AND next_page > 0 AND failure_count >= 0 AND credential_version >= 0', name='ck_build_scene_job_counters'),
    sa.ForeignKeyConstraint(['actor_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['dispatch_id'], ['pending_dispatch.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'advertiser_id'], ['advertiser_account.tenant_id', 'advertiser_account.advertiser_id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'bc_id', 'capability_job_id'], ['account_capability_job.tenant_id', 'account_capability_job.bc_id', 'account_capability_job.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'bc_id'], ['tenant_bc.tenant_id', 'tenant_bc.bc_id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'connection_id'], ['tiktok_connection.tenant_id', 'tiktok_connection.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'provider_connection_id', 'application_id'], ['provider_application.tenant_id', 'provider_application.connection_id', 'provider_application.external_id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'bc_id', 'advertiser_id', 'id', name='uq_build_scene_job_account'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_build_scene_job_tenant')
    )
    op.create_index('ix_build_scene_job_latest', 'build_scene_job', ['tenant_id', 'scope_basis', 'created_at', 'id'], unique=False)
    op.create_index('ix_build_scene_job_repair', 'build_scene_job', ['status', 'repair_after', 'id'], unique=False)
    op.create_index('uq_build_scene_job_active_basis', 'build_scene_job', ['tenant_id', 'scope_basis'], unique=True, postgresql_where=sa.text("status = 'PENDING'"))
    op.create_table('build_scene_job_page',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('resource', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
    sa.Column('page', sa.Integer(), nullable=False),
    sa.Column('endpoint', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('request_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('source_revision', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('scope_basis', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('facts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("jsonb_typeof(facts) = 'object'", name='ck_build_scene_job_page_facts'),
    sa.CheckConstraint("resource IN ('identity','minis','cta','vbo','regions')", name='ck_build_scene_job_page_resource'),
    sa.CheckConstraint('page > 0', name='ck_build_scene_job_page_number'),
    sa.ForeignKeyConstraint(['tenant_id', 'job_id'], ['build_scene_job.tenant_id', 'build_scene_job.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'resource', 'page', name='uq_build_scene_job_page')
    )
    op.create_table('draft_scene_preparation',
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('preparation_id', sa.Uuid(), nullable=False),
    sa.Column('draft_id', sa.Uuid(), nullable=False),
    sa.Column('connection_after', sa.Uuid(), nullable=True),
    sa.Column('capabilities_queued', sa.Boolean(), nullable=False),
    sa.Column('capabilities_complete', sa.Boolean(), nullable=False),
    sa.Column('scene_after', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('scenes_queued', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'draft_id', 'preparation_id'], ['draft_preparation.tenant_id', 'draft_preparation.draft_id', 'draft_preparation.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'preparation_id'),
    sa.UniqueConstraint('tenant_id', 'draft_id', 'preparation_id', name='uq_draft_scene_preparation_scope')
    )
    op.create_table('draft_capability_dependency',
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('preparation_id', sa.Uuid(), nullable=False),
    sa.Column('connection_id', sa.Uuid(), nullable=False),
    sa.Column('draft_id', sa.Uuid(), nullable=False),
    sa.Column('bc_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('error_code', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.CheckConstraint("status IN ('PENDING','COMPLETE','BLOCKED')", name='ck_draft_capability_dependency_status'),
    sa.ForeignKeyConstraint(['tenant_id', 'bc_id', 'job_id'], ['account_capability_job.tenant_id', 'account_capability_job.bc_id', 'account_capability_job.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'connection_id'], ['tiktok_connection.tenant_id', 'tiktok_connection.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'draft_id', 'preparation_id'], ['draft_scene_preparation.tenant_id', 'draft_scene_preparation.draft_id', 'draft_scene_preparation.preparation_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'preparation_id', 'connection_id')
    )
    op.create_index('ix_draft_capability_dependency_pending', 'draft_capability_dependency', ['tenant_id', 'preparation_id', 'status', 'connection_id'], unique=False)
    op.create_table('draft_scene_dependency',
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('preparation_id', sa.Uuid(), nullable=False),
    sa.Column('advertiser_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('draft_id', sa.Uuid(), nullable=False),
    sa.Column('bc_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('error_code', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.CheckConstraint("status IN ('PENDING','COMPLETE','BLOCKED')", name='ck_draft_scene_dependency_status'),
    sa.ForeignKeyConstraint(['tenant_id', 'bc_id', 'advertiser_id', 'job_id'], ['build_scene_job.tenant_id', 'build_scene_job.bc_id', 'build_scene_job.advertiser_id', 'build_scene_job.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'draft_id', 'advertiser_id'], ['draft_account.tenant_id', 'draft_account.draft_id', 'draft_account.advertiser_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id', 'draft_id', 'preparation_id'], ['draft_scene_preparation.tenant_id', 'draft_scene_preparation.draft_id', 'draft_scene_preparation.preparation_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'preparation_id', 'advertiser_id')
    )
    op.create_index('ix_draft_scene_dependency_pending', 'draft_scene_dependency', ['tenant_id', 'preparation_id', 'status', 'advertiser_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_draft_scene_dependency_pending', table_name='draft_scene_dependency')
    op.drop_table('draft_scene_dependency')
    op.drop_index('ix_draft_capability_dependency_pending', table_name='draft_capability_dependency')
    op.drop_table('draft_capability_dependency')
    op.drop_table('draft_scene_preparation')
    op.drop_table('build_scene_job_page')
    op.drop_index('uq_build_scene_job_active_basis', table_name='build_scene_job', postgresql_where=sa.text("status = 'PENDING'"))
    op.drop_index('ix_build_scene_job_repair', table_name='build_scene_job')
    op.drop_index('ix_build_scene_job_latest', table_name='build_scene_job')
    op.drop_table('build_scene_job')
