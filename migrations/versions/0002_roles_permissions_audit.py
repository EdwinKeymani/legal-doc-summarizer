"""Roles, permissions and the audit log (increment 7).

Adds:
  * role, permission, role_permission: the access policy, seeded with the
    three roles, sixteen permissions and the links between them
  * audit_log: the permanent record of who did what
  * user.role_id (every existing account becomes a Member) and user.is_active
  * user_preference.sidebar_collapsed

The seed data is written out here on purpose rather than imported from the
app: a migration must keep doing exactly the same thing even after the app's
code changes.

Revision ID: 0002_roles_permissions_audit
Revises: 0001_baseline
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_roles_permissions_audit"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

USER_ROLE_FOREIGN_KEY_NAME = "fk_user_role_id_role"

SEED_ROLES = [
    # (name, label, description)
    ("member", "Member",
     "A client of the clinic. Uploads contracts, reads the reports and can ask for a human review."),
    ("reviewer", "Reviewer",
     "A law student, paralegal or advocate who checks the AI's ratings on contracts assigned to them."),
    ("admin", "Administrator",
     "The clinic coordinator. Manages people, assigns reviews and oversees the clinic; does not read contract text."),
]

SEED_PERMISSIONS = [
    # (code_name, description, role that holds it)
    ("document.upload", "Upload and analyse a contract", "member"),
    ("document.view_own", "Open own reports and re-run the risk assessment", "member"),
    ("document.export_own", "Download own reports (PDF, Word) and original files", "member"),
    ("document.delete_own", "Delete own documents", "member"),
    ("review.request", "Ask the clinic for a human review", "member"),
    ("review.withdraw_own", "Withdraw own review request before it is completed", "member"),
    ("review.view_queue", "See the reviews assigned to me", "reviewer"),
    ("document.view_assigned", "Open the full report of a document assigned to me", "reviewer"),
    ("review.perform", "Confirm or change clause ratings and complete a review", "reviewer"),
    ("review.view_all", "See every review request: requester, file name, status, reviewer", "admin"),
    ("review.assign", "Assign or reassign a reviewer", "admin"),
    ("user.view_all", "See the list of accounts", "admin"),
    ("user.change_role", "Change a person's role", "admin"),
    ("user.deactivate", "Deactivate or restore an account", "admin"),
    ("audit.view", "Read the audit log", "admin"),
    ("stats.view", "See clinic statistics", "admin"),
]

role_table = sa.table(
    "role", sa.column("id", sa.Integer), sa.column("name", sa.String),
    sa.column("label", sa.String), sa.column("description", sa.String),
)
permission_table = sa.table(
    "permission", sa.column("id", sa.Integer), sa.column("code_name", sa.String),
    sa.column("description", sa.String),
)
role_permission_table = sa.table(
    "role_permission", sa.column("role_id", sa.Integer), sa.column("permission_id", sa.Integer),
)
user_table = sa.table("user", sa.column("id", sa.Integer), sa.column("role_id", sa.Integer))


def column_names(table_name):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    tables_already_present = set(sa.inspect(bind).get_table_names())

    # ---- The access policy ----
    if "role" not in tables_already_present:
        op.create_table(
            "role",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=20), nullable=False),
            sa.Column("label", sa.String(length=40), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
    if "permission" not in tables_already_present:
        op.create_table(
            "permission",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code_name", sa.String(length=60), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code_name"),
        )
    if "role_permission" not in tables_already_present:
        op.create_table(
            "role_permission",
            sa.Column("role_id", sa.Integer(), nullable=False),
            sa.Column("permission_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
            sa.ForeignKeyConstraint(["permission_id"], ["permission.id"]),
            sa.PrimaryKeyConstraint("role_id", "permission_id"),
        )

    # Seed data: only into empty tables, so running this twice adds nothing
    if bind.execute(sa.select(sa.func.count()).select_from(role_table)).scalar() == 0:
        op.bulk_insert(role_table, [
            {"name": name, "label": label, "description": description}
            for name, label, description in SEED_ROLES
        ])
    if bind.execute(sa.select(sa.func.count()).select_from(permission_table)).scalar() == 0:
        op.bulk_insert(permission_table, [
            {"code_name": code_name, "description": description}
            for code_name, description, _ in SEED_PERMISSIONS
        ])
    if bind.execute(sa.select(sa.func.count()).select_from(role_permission_table)).scalar() == 0:
        role_ids = dict(bind.execute(sa.select(role_table.c.name, role_table.c.id)).all())
        permission_ids = dict(bind.execute(sa.select(permission_table.c.code_name, permission_table.c.id)).all())
        op.bulk_insert(role_permission_table, [
            {"role_id": role_ids[role_name], "permission_id": permission_ids[code_name]}
            for code_name, _, role_name in SEED_PERMISSIONS
        ])

    # ---- The audit log ----
    if "audit_log" not in tables_already_present:
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("actor_role", sa.String(length=20), nullable=True),
            sa.Column("action", sa.String(length=60), nullable=False),
            sa.Column("target_type", sa.String(length=40), nullable=True),
            sa.Column("target_id", sa.Integer(), nullable=True),
            sa.Column("details", sa.Text(), nullable=True),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
        op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])
        op.create_index("ix_audit_log_action", "audit_log", ["action"])

    # ---- New columns on existing tables ----
    existing_user_columns = column_names("user")
    if "is_active" not in existing_user_columns:
        op.add_column("user", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    if "role_id" not in existing_user_columns:
        # Added as optional, filled in for every existing account, then made required
        op.add_column("user", sa.Column("role_id", sa.Integer(), nullable=True))
        member_role_id = bind.execute(sa.select(role_table.c.id).where(role_table.c.name == "member")).scalar_one()
        bind.execute(user_table.update().where(user_table.c.role_id.is_(None)).values(role_id=member_role_id))
        with op.batch_alter_table("user") as batch_operation:
            batch_operation.alter_column("role_id", existing_type=sa.Integer(), nullable=False)
            batch_operation.create_foreign_key(USER_ROLE_FOREIGN_KEY_NAME, "role", ["role_id"], ["id"])
            batch_operation.create_index("ix_user_role_id", ["role_id"])

    if "sidebar_collapsed" not in column_names("user_preference"):
        op.add_column("user_preference", sa.Column(
            "sidebar_collapsed", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    """Back to increment 6. Roles and the audit log are deleted; accounts,
    documents and preferences are kept."""
    with op.batch_alter_table("user_preference") as batch_operation:
        batch_operation.drop_column("sidebar_collapsed")
    with op.batch_alter_table("user") as batch_operation:
        batch_operation.drop_index("ix_user_role_id")
        batch_operation.drop_constraint(USER_ROLE_FOREIGN_KEY_NAME, type_="foreignkey")
        batch_operation.drop_column("role_id")
        batch_operation.drop_column("is_active")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_occurred_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("role_permission")
    op.drop_table("permission")
    op.drop_table("role")
