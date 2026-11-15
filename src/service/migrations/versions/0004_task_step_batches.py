"""Append execution details separately from frequently updated task headers."""
from alembic import op
import sqlalchemy as sa

revision = "0004_task_step_batches"
down_revision = "0003_ip_pool"
branch_labels = depends_on = None


def upgrade():
    op.create_table("task_step_batches", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("task_id", sa.String(), nullable=False),
                    sa.Column("account_id", sa.String(), nullable=False),
                    sa.Column("records", sa.JSON(), nullable=False))
    op.create_index("ix_task_step_batches_task_account", "task_step_batches", ["task_id", "account_id"])


def downgrade():
    connection = op.get_bind()
    tasks = sa.table("tasks", sa.column("task_id", sa.String), sa.column("record", sa.JSON))
    batches = sa.table("task_step_batches", sa.column("id", sa.Integer),
                       sa.column("task_id", sa.String), sa.column("account_id", sa.String),
                       sa.column("records", sa.JSON))
    for task_id, record in connection.execute(sa.select(tasks)):
        changed = False
        for row in record.get("account_results", []):
            if "_step_results_streamed" not in row:
                continue
            changed = True
            if row.pop("_step_results_streamed"):
                query = sa.select(batches.c.records).where(batches.c.task_id == task_id,
                    batches.c.account_id == row["account_id"]).order_by(batches.c.id)
                row["step_results"] = [step for batch in connection.execute(query) for step in batch[0]]
        if changed:
            connection.execute(tasks.update().where(tasks.c.task_id == task_id).values(record=record))
    op.drop_table("task_step_batches")
