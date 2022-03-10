"""Add content_type column to items

Revision ID: 48cfe99f5c21
Revises: be804e93d51d
Create Date: 2022-01-26 14:46:07.469573

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "48cfe99f5c21"
down_revision = "be804e93d51d"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "items",
        sa.Column("content_type", sa.String(), nullable=True),
    )
    # ### end Alembic commands ###


def downgrade():
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_column("content_type")