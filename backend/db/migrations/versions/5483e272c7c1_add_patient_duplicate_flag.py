"""add patient duplicate flag

Revision ID: 5483e272c7c1
Revises: 20260912141027
Create Date: 2026-09-13 00:54:49.390920

Patients page follow-up (Section 0): flags a newly-created `patients` row
as a possible duplicate when it matches another ACTIVE patient in the same
hospital on at least 3 of {name, phone, date_of_birth, gender} -- see
db/repositories/patients.py's _flag_duplicate_if_matches(), called from both
_upsert_patient() (appointments.py) and create_patient_profile() right after
a brand-new row is inserted. duplicate_of_patient_id points at whichever
existing patient it matched (SET NULL if that patient is ever deleted, so
this never blocks a delete); duplicate_flag_reason is a human-readable
summary of which fields matched vs. differed, e.g. "Matches patient
DCCP-2026-004 on name, gender, phone -- date of birth differs". Purely
informational for now -- just a column on the portal's patient list for
staff to review, no merge/dismiss workflow yet.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5483e272c7c1'
down_revision: Union[str, None] = '20260912141027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("duplicate_of_patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="SET NULL"), nullable=True))
    op.add_column("patients", sa.Column("duplicate_flag_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("patients", "duplicate_flag_reason")
    op.drop_column("patients", "duplicate_of_patient_id")
