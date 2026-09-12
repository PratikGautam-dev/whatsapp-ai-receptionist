# db/orm_models.py
"""SQLAlchemy ORM model classes, added one domain at a time as each
db/repositories/*.py file migrates off raw SQL (see the SQLAlchemy ORM +
Alembic migration plan). These map onto tables that already exist --
created/versioned by db/schema.sql + db/migrations/ -- never by
Base.metadata.create_all(); a new column still needs its own Alembic
revision, adding it here alone changes nothing in the database.

Datetime columns are mapped as String, not DateTime: every timestamp in this
schema is stored as ISO-8601 TEXT (db/schema.sql's own header comment
explains why), not a native Postgres TIMESTAMP -- mapping them as DateTime
here would silently change how SQLAlchemy binds/reads these columns from
what every existing raw-SQL repository function already does.

ForeignKey() below mirrors constraints db/schema.sql already enforces in
Postgres -- it's metadata only (documents cardinality/relations so they're
visible by reading this file), not a behavior change: no relationship()
pairs, no cascade, no lazy-loading. A column only gets ForeignKey() here if
the table actually has that REFERENCES clause in schema.sql; a few TEXT
columns that look like foreign keys (e.g. appointments.appointment_type_id)
aren't DB-enforced and are deliberately left plain to match."""
from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CareConnectAccount(Base):
    """db/schema.sql's care_connect_accounts table -- see that file's own
    comment for the "why global, not hospital-scoped" identity-layer
    reasoning. status/created_at are Postgres-side defaults ('active',
    now()::text) -- always created via a Core `insert(CareConnectAccount)
    .values()` with NO columns set (never the ORM's own instance-persistence
    path), matching the original `INSERT ... DEFAULT VALUES` exactly; the
    ORM's own new-instance INSERT path sends an explicit NULL for any unset
    column instead of omitting it, which would violate this table's NOT NULL
    constraints -- verified empirically before relying on it."""
    __tablename__ = "care_connect_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str]
    created_at: Mapped[str]
    updated_at: Mapped[str | None]
    # migration 0006 -- global, id-derived (db/display_ids.py), not yet
    # surfaced in any UI; see patients.patient_display_id for the precedent.
    # Nullable at the DB level only for the same "INSERT can't know its own
    # id yet" reason patient_display_id is -- always set in practice by the
    # time any caller reads the row (see that migration's own docstring).
    display_id: Mapped[str | None]
    # Language-persistence follow-up (confirmed with the user): a chosen
    # language is GLOBAL to the account (same language at every hospital
    # this person messages), not per-hospital like dpdp_consents --
    # language is a personal preference, not a hospital-specific compliance
    # matter. NULL means "never chosen yet" -- flows/router.py's
    # _enter_idle() still shows the picker in that case, same as before
    # this column existed.
    language: Mapped[str | None]


class WhatsappIdentity(Base):
    """db/schema.sql's whatsapp_identities table. Same Core-insert-only
    caveat as CareConnectAccount above applies to created_at.
    care_connect_account_id is UNIQUE in the DB -- genuinely 1:1 with
    CareConnectAccount today, not 1:M (modeled as a separate table anyway to
    leave room for a future second channel identity per account)."""
    __tablename__ = "whatsapp_identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    care_connect_account_id: Mapped[int] = mapped_column(ForeignKey("care_connect_accounts.id"), unique=True)
    provider_user_id: Mapped[str]
    username: Mapped[str | None]
    phone_number: Mapped[str | None]
    created_at: Mapped[str]
    updated_at: Mapped[str | None]


class AppointmentType(Base):
    """db/schema.sql's appointment_types table -- one row per (hospital, type
    id), PRIMARY KEY (hospital_id, id) matching the table's own composite
    key. is_active/requires_consent/requires_doctor_selection all have
    Postgres-side defaults, but every write path today (db/init_db.py,
    db/seed.py, db/repositories/hospitals.py) always sets every column
    explicitly, so unlike CareConnectAccount above there's no unset-column-
    vs-server-default pitfall to worry about here -- nothing currently
    inserts a row via this model regardless (see appointment_types.py, read-
    only so far)."""
    __tablename__ = "appointment_types"

    id: Mapped[str] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), primary_key=True)
    label: Mapped[str]
    requires_consent: Mapped[bool]
    requires_doctor_selection: Mapped[bool]
    is_active: Mapped[bool]
    # Platform-admin-controlled whitelist (edit-tenant page): whether this
    # tenant may use this type AT ALL. is_active is the tenant's own portal-
    # level on/off switch WITHIN that whitelist -- a type can never be
    # is_active=True while is_allowed=False, enforced in
    # db/repositories/appointment_types.py's set_appointment_type_active().
    is_allowed: Mapped[bool]
    sort_order: Mapped[int]


class Procedure(Base):
    """db/schema.sql's procedures table (Daycare/Procedure rebuild) -- the
    hospital-configurable procedure catalog (Step 1's list, Step 2's booking
    mode)."""
    __tablename__ = "procedures"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    category: Mapped[str]
    name: Mapped[str]
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"))
    booking_mode: Mapped[str]
    duration_minutes: Mapped[int]
    estimated_price_min: Mapped[float | None] = mapped_column(Numeric(10, 2))
    estimated_price_max: Mapped[float | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool]
    sort_order: Mapped[int]


class ProcedureRequiredResourceType(Base):
    """db/schema.sql's procedure_required_resource_types table -- which
    resource pools (bed_chair/equipment/staff) a procedure needs, combined."""
    __tablename__ = "procedure_required_resource_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    procedure_id: Mapped[int] = mapped_column(ForeignKey("procedures.id"))
    resource_type: Mapped[str]


class ProcedureInstruction(Base):
    """db/schema.sql's procedure_instructions table -- Step 5's hospital-
    configured pre-procedure instructions, one row per line item."""
    __tablename__ = "procedure_instructions"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    procedure_id: Mapped[int] = mapped_column(ForeignKey("procedures.id"))
    instruction_type: Mapped[str]
    instruction_text: Mapped[str]
    sort_order: Mapped[int]


class ProcedureResource(Base):
    """db/schema.sql's procedure_resources table -- one row per physical
    bed/chair, machine, or nursing/technician slot-line. Schedule columns are
    a direct clone of DiagnosticResource's own grid, resource_type-discriminated."""
    __tablename__ = "procedure_resources"

    id: Mapped[str] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    resource_type: Mapped[str]
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"))
    name: Mapped[str]
    working_days: Mapped[str]
    working_hours: Mapped[str]
    slot_duration_minutes: Mapped[int]
    breaks: Mapped[str]
    max_bookings_per_slot: Mapped[int]
    daily_booking_limit: Mapped[int | None]
    effective_from: Mapped[str | None]
    is_active: Mapped[bool]


class ProcedureResourceLeave(Base):
    """db/schema.sql's procedure_resource_leave table -- mirrors DiagnosticResourceLeave."""
    __tablename__ = "procedure_resource_leave"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    resource_id: Mapped[str] = mapped_column(ForeignKey("procedure_resources.id"))
    date: Mapped[str]
    reason: Mapped[str | None]


class ProcedureResourceSlot(Base):
    """db/schema.sql's procedure_resource_slots table -- mirrors DiagnosticResourceSlot."""
    __tablename__ = "procedure_resource_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    resource_id: Mapped[str] = mapped_column(ForeignKey("procedure_resources.id"))
    scheduled_at: Mapped[str]
    blocked: Mapped[bool]
    block_reason: Mapped[str | None]


class AppointmentProcedureResource(Base):
    """db/schema.sql's appointment_procedure_resources table -- N concrete
    resources bound to one procedure appointment (bed #3 + IV pump #1 +
    nurse-line B, all at the same scheduled_at), same "N rows per one
    appointment" shape as AppointmentLabTest."""
    __tablename__ = "appointment_procedure_resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"))
    resource_id: Mapped[str] = mapped_column(ForeignKey("procedure_resources.id"))
    resource_type: Mapped[str]
    resource_name: Mapped[str]


class DiagnosticTestLeave(Base):
    """db/schema.sql's diagnostic_test_leave table -- mirrors DoctorLeave.
    Diagnostic tests/resources merge: a test IS the bookable
    machine/equipment now, so this is keyed on test_id directly (no more
    separate diagnostic_resources table)."""
    __tablename__ = "diagnostic_test_leave"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    test_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_tests.id"))
    date: Mapped[str]
    reason: Mapped[str | None]


class DiagnosticTestSlot(Base):
    """db/schema.sql's diagnostic_test_slots table -- mirrors DoctorSlot.
    See DiagnosticTestLeave's docstring for the test_id-instead-of-
    resource_id rationale."""
    __tablename__ = "diagnostic_test_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    test_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_tests.id"))
    scheduled_at: Mapped[str]
    blocked: Mapped[bool]
    block_reason: Mapped[str | None]


class DiagnosticTest(Base):
    """db/schema.sql's diagnostic_tests table -- the catalog a patient picks
    from for Diagnostic Test / Lab Test (category discriminates the two).
    Diagnostic tests/resources merge: a test now carries its own schedule
    directly (no separate diagnostic_resources row, no department -- a
    diagnostic/lab booking never has a department at all). Test/variant
    merge: price lives directly here too now -- a test only ever needed
    exactly one priced option, so the separate diagnostic_test_variants
    child table (and its unused preparation_instructions field) is gone."""
    __tablename__ = "diagnostic_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    category: Mapped[str]
    name: Mapped[str]
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    working_days: Mapped[str]
    working_hours: Mapped[str]
    slot_duration_minutes: Mapped[int]
    breaks: Mapped[str]
    max_bookings_per_slot: Mapped[int]
    daily_booking_limit: Mapped[int | None]
    effective_from: Mapped[str | None]
    is_active: Mapped[bool]
    sort_order: Mapped[int]


class LabServiceArea(Base):
    """db/schema.sql's lab_service_areas table -- hospital-configurable list
    of PIN codes serviceable for Lab Test home sample collection. A row is
    either a single pincode OR a range_start/range_end pair, never both."""
    __tablename__ = "lab_service_areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    pincode: Mapped[str | None]
    range_start: Mapped[str | None]
    range_end: Mapped[str | None]
    is_active: Mapped[bool]


class AppointmentLabTest(Base):
    """db/schema.sql's appointment_lab_tests table -- the Lab Test basket: N
    rows per one `appointments` row, one per selected test. test_label/price
    snapshot columns follow the same denormalization convention as
    appointments.diagnostic_test_label."""
    __tablename__ = "appointment_lab_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"))
    diagnostic_test_id: Mapped[int | None] = mapped_column(ForeignKey("diagnostic_tests.id"))
    test_label: Mapped[str]
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))


class FaqTopic(Base):
    """db/schema.sql's faq_topics table -- the faq_flow_type's entire data
    model (SPEC Section 14.2)."""
    __tablename__ = "faq_topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    topic_label: Mapped[str]
    answer_text: Mapped[str]
    display_order: Mapped[int]


class DoctorLeave(Base):
    """db/schema.sql's doctor_leave table -- one row per date a doctor is
    unavailable for the whole day (Section 14.7)."""
    __tablename__ = "doctor_leave"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    doctor_id: Mapped[str] = mapped_column(ForeignKey("doctors.id"))
    date: Mapped[str]
    reason: Mapped[str | None]


class DoctorSlotOverride(Base):
    """db/schema.sql's doctor_slot_overrides table (migration 0032) --
    replaces the old doctor_slots table's "one row per every possible slot,
    pre-generated ahead of time" shape (found to scale badly and be the root
    cause of a stale-window bug -- confirmed with the user) with "one row
    only for a slot staff has actually touched." A doctor's normal bookable
    grid is now computed live from working_days/working_hours/
    slot_duration_minutes/breaks/doctor_leave (db/repositories/doctors.py's
    _compute_candidate_slots()) -- this table only ever holds exceptions:
    - is_custom=True: a one-off extra slot OUTSIDE that normal pattern
      (portal's "Add slot"), which the live computation would never produce
      on its own.
    - blocked=True: this scheduled_at (whether a normal-pattern slot or a
      custom one) must never be offered (portal's "Block slot"), but still
      shows up in the admin view so staff can unblock it later.
    - excluded=True (migration 0034): this scheduled_at is gone outright
      (portal's "Remove slot") -- dropped from every view, not just hidden
      from booking, which is what distinguishes it from blocked=True.
    A row can combine is_custom/blocked/excluded -- a row with none of the
    three True has no reason to exist and is deleted rather than kept
    (db/repositories/slots.py's set_slot_blocked())."""
    __tablename__ = "doctor_slot_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    doctor_id: Mapped[str] = mapped_column(ForeignKey("doctors.id"))
    scheduled_at: Mapped[str]
    is_custom: Mapped[bool]
    blocked: Mapped[bool]
    block_reason: Mapped[str | None]
    excluded: Mapped[bool]


class Department(Base):
    """db/schema.sql's departments table. id is a caller-generated opaque
    string (h{hospital_id}_{uuid}), not a DB-assigned SERIAL -- see
    create_department()'s own docstring.

    Migration 20260912141027 (Settings -> Departments tab): floor_wing/
    consultation_hours/description/head_doctor_id are the profile fields;
    is_active is the internal status; show_on_frontend/
    whatsapp_booking_enabled both gate the one real patient channel this
    app has (the WhatsApp department picker -- see get_departments());
    online_booking_enabled is stored/toggleable only, since no separate
    online booking channel exists anywhere in this codebase yet to enforce
    it in (confirmed with the user, not an oversight)."""
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    name: Mapped[str]
    floor_wing: Mapped[str | None]
    consultation_hours: Mapped[str | None]
    description: Mapped[str | None]
    head_doctor_id: Mapped[str | None] = mapped_column(ForeignKey("doctors.id", ondelete="SET NULL"))
    is_active: Mapped[bool]
    show_on_frontend: Mapped[bool]
    online_booking_enabled: Mapped[bool]
    whatsapp_booking_enabled: Mapped[bool]


class DoctorRow(Base):
    """db/schema.sql's doctors table -- the FULL, authoritative mapping, per
    doctors.py's own migration (slots.py originally added this as a partial
    model with just max_bookings_per_slot; extended here to every column,
    per that model's own "doctors.py's own migration should define the
    complete model" instruction). Named DoctorRow, not Doctor, to leave that
    name free for a future dataclass -- see UserAccount's docstring for the
    same naming precedent."""
    __tablename__ = "doctors"

    id: Mapped[str] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    name: Mapped[str]
    # Migration 20260911190007: the Doctors page's mocked columns (phone,
    # employee ID, location) becoming real -- phone/employee_id join
    # specialization/qualification as mandatory fields (confirmed with the
    # user); location stays optional.
    specialization: Mapped[str]
    qualification: Mapped[str]
    years_experience: Mapped[int | None]
    working_days: Mapped[str]
    working_hours: Mapped[str]
    slot_duration_minutes: Mapped[int]
    breaks: Mapped[str]
    max_bookings_per_slot: Mapped[int]
    daily_booking_limit: Mapped[int | None]
    online_quota: Mapped[int | None]
    walkin_quota: Mapped[int | None]
    followup_duration_minutes: Mapped[int | None]
    effective_from: Mapped[str | None]
    is_active: Mapped[bool]
    phone: Mapped[str]
    employee_id: Mapped[str]
    location: Mapped[str | None]
    # Migration 0033: a QUEUED future schedule change -- when a schedule
    # edit's effective_from is still in the future, the submitted pattern
    # goes here instead of overwriting the columns above immediately, so the
    # CURRENT pattern keeps being served for near-term dates until
    # pending_effective_from arrives (db/repositories/doctors.py's
    # compute_doctor_candidate_slots() picks whichever pattern applies to
    # each computed date, purely by comparing dates -- no promotion job
    # needed). NULL pending_effective_from means no change is queued.
    pending_working_days: Mapped[str | None]
    pending_working_hours: Mapped[str | None]
    pending_slot_duration_minutes: Mapped[int | None]
    pending_breaks: Mapped[str | None]
    pending_daily_booking_limit: Mapped[int | None]
    pending_effective_from: Mapped[str | None]


class AppointmentRow(Base):
    """db/schema.sql's appointments table -- the FULL, authoritative mapping,
    per appointments.py's own migration (closing the "partial, extend later"
    deferrals slots.py and patients.py both left on this model). Named
    AppointmentRow, not Appointment, since db/models.py already has an
    Appointment dataclass with a different (fuller, JOINED-with-department/
    doctor/patient) shape -- _row_to_appointment() in that module still maps
    the JOIN result (raw or ORM) onto that dataclass unchanged.

    _upsert_patient()/create_appointment() (db/repositories/appointments.py)
    do NOT use this model for their writes -- see those functions' own
    docstrings (advisory-lock + multi-statement-transaction code stays raw
    SQL permanently, same reasoning as patients.py's create_patient_profile()
    trio). appointment_type_id is deliberately plain (no ForeignKey()) --
    schema.sql adds that column with no REFERENCES clause, so it isn't
    DB-enforced against appointment_types' composite (hospital_id, id) key."""
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    phone: Mapped[str]
    # Migration 0035: nullable for the same reason doctor_id below is -- a
    # resource-bound booking's diagnostic_resources/procedure_resources row
    # can itself have no department configured.
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"))
    # Diagnostic/Lab Phase 2: nullable -- a resource-bound booking
    # (diagnostic_test_id set) has no doctor at all.
    # appointments_doctor_or_resource_or_procedure_chk enforces at least one
    # of doctor_id/diagnostic_test_id/procedure_id is set.
    doctor_id: Mapped[str | None] = mapped_column(ForeignKey("doctors.id"))
    scheduled_at: Mapped[str]
    status: Mapped[str]
    source: Mapped[str]
    booking_ordinal: Mapped[int]
    reference_id: Mapped[str | None]
    created_at: Mapped[str]
    updated_at: Mapped[str | None]
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"))
    patient_name: Mapped[str | None]
    patient_phone: Mapped[str | None]
    # Family/multi-person-booking follow-up (Spec.md Section 0): denormalized
    # snapshot for the legacy duplicate-booking check that tells apart two
    # family members sharing one phone (was patient_age/patients.age --
    # replaced by the age-to-DOB migration, see that migration's own
    # docstring).
    patient_date_of_birth: Mapped[str | None]
    deleted_at: Mapped[str | None]
    appointment_type_id: Mapped[str | None]
    consent_given_at: Mapped[str | None]
    video_link: Mapped[str | None]
    # Follow-up validity override (migration 0024) -- see db/schema.sql's own
    # column comment. NULL means no override has ever been granted.
    followup_override_until: Mapped[str | None]
    # Diagnostic/Lab Phase 2: the diagnostic_tests row this booking is bound
    # to for scheduling purposes (None for every doctor-bound appointment
    # type). Diagnostic tests/resources merge: this now points straight at
    # diagnostic_tests.id -- for a Lab Test basket booking it's whichever
    # basket item anchors the slot (see flows/booking/types/lab.py).
    # diagnostic_test_label/diagnostic_price are snapshots at booking time,
    # same denormalization convention as patient_name/patient_phone.
    #
    # This used to be two columns (resource_id -- the one actually wired
    # into the double-booking/advisory-lock check, the unique index, and the
    # doctor-or-resource-or-procedure CHECK constraint -- plus a second,
    # fully-redundant diagnostic_test_id always set to the same value or
    # left NULL). Merged into one under this name; see migration
    # 20260911135450's own docstring.
    diagnostic_test_id: Mapped[int | None] = mapped_column(ForeignKey("diagnostic_tests.id"))
    diagnostic_test_label: Mapped[str | None]
    diagnostic_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    # Lab Test Phase 2 follow-up: collection details + the post-booking
    # report lifecycle, only ever set for a Lab Test booking -- see
    # db/schema.sql's own column comments. The basket itself lives in
    # AppointmentLabTest, not here.
    collection_method: Mapped[str | None]
    collection_address: Mapped[str | None]
    collection_pincode: Mapped[str | None]
    home_collection_charge: Mapped[float | None] = mapped_column(Numeric(10, 2))
    lab_status: Mapped[str | None]
    # Daycare/Procedure rebuild: which catalog procedure, its own approval
    # sub-status (None for every other appointment type), a price-at-booking
    # snapshot, an optional order/prescription reference, and a pending
    # "Request Reschedule" slot -- see db/schema.sql's own column comments.
    # The bound resources (N rows) live in AppointmentProcedureResource, not here.
    procedure_id: Mapped[int | None] = mapped_column(ForeignKey("procedures.id"))
    procedure_status: Mapped[str | None]
    procedure_estimated_price_min: Mapped[float | None] = mapped_column(Numeric(10, 2))
    procedure_estimated_price_max: Mapped[float | None] = mapped_column(Numeric(10, 2))
    procedure_order_reference: Mapped[str | None]
    procedure_reschedule_requested_at: Mapped[str | None]


class AppointmentReminder(Base):
    """db/schema.sql's appointment_reminders table."""
    __tablename__ = "appointment_reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"))
    offset_hours: Mapped[float]
    sent_at: Mapped[str]


# UserAccount ("users" table) and HospitalUser ("hospital_users" table)
# used to live here -- Google-OAuth accounts and the hospital-ownership join
# table (Section 15). Migration 0016 replaced them with Identity/
# HospitalOwner; migration 0017 dropped both tables once the new ones were
# verified correct in production. See Identity's own docstring.


class HandoffRequest(Base):
    """db/schema.sql's handoff_requests table -- the human-handoff queue
    (see that file's own comment for the two unrelated triggers that share
    this one table)."""
    __tablename__ = "handoff_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    phone: Mapped[str]
    reason: Mapped[str]
    message_text: Mapped[str | None]
    status: Mapped[str]
    created_at: Mapped[str]
    resolved_at: Mapped[str | None]
    deleted_at: Mapped[str | None]
    resolved_by: Mapped[str | None]


class HandoffMessage(Base):
    """db/schema.sql's handoff_messages table -- real two-way conversation
    threading for an active handoff."""
    __tablename__ = "handoff_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    handoff_request_id: Mapped[int] = mapped_column(ForeignKey("handoff_requests.id"))
    direction: Mapped[str]
    message_text: Mapped[str]
    created_at: Mapped[str]


class HospitalRow(Base):
    """db/schema.sql's hospitals table -- the FULL, authoritative mapping
    (every column the table has), named HospitalRow (not Hospital, which is
    db/models.py's dataclass _row_to_hospital() below maps onto). This is
    the model db/repositories/users.py's get_hospitals_for_user() deferred
    to, per that function's own docstring -- update that deferral note if
    this model's shape ever changes in a way that affects it.

    is_active/language_prompt_enabled are genuinely INTEGER columns in
    Postgres (not BOOLEAN) -- db/schema.sql's own column definitions --
    mapped as int here to match; require_patient_confirmation/
    dpdp_consent_required ARE real BOOLEAN columns. Mixed on purpose,
    matching the schema exactly rather than normalizing them to look
    consistent."""
    __tablename__ = "hospitals"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    whatsapp_phone_number_id: Mapped[str | None]
    meta_access_token_ref: Mapped[str | None]
    app_secret_ref: Mapped[str | None]
    timezone: Mapped[str]
    welcome_message_text: Mapped[str | None]
    reminder_offsets_hours: Mapped[str]
    reminder_template_name: Mapped[str | None]
    data_tier: Mapped[str]
    external_api_base_url: Mapped[str | None]
    external_api_key: Mapped[str | None]
    portal_password_hash: Mapped[str | None]
    flow_type: Mapped[str]
    enabled_features: Mapped[str | None]
    feature_labels: Mapped[str | None]
    closing_message_text: Mapped[str | None]
    business_hours_text: Mapped[str | None]
    default_language: Mapped[str | None]
    language_prompt_enabled: Mapped[int | None]
    session_timeout_minutes: Mapped[int | None]
    handoff_auto_resolve_hours: Mapped[int | None]
    is_active: Mapped[int]
    created_at: Mapped[str]
    patient_id_prefix: Mapped[str | None]
    require_patient_confirmation: Mapped[bool]
    privacy_notice_text: Mapped[str | None]
    dpdp_consent_required: Mapped[bool]
    tenant_type: Mapped[str]
    admin_capabilities: Mapped[str | None]
    # migration 0006 -- global, id-derived (db/display_ids.py); shown to
    # hospital users the same way patients.patient_display_id is shown to
    # patients. Nullable at the DB level for the same reason
    # CareConnectAccount.display_id is -- see that model's own comment.
    display_id: Mapped[str | None]
    # Migration 20260912065049 -- Leave Requests admin page's portal-admin-
    # configurable policy (doctor/receptionist only, confirmed with the
    # user; admin approves leave rather than accruing an allowance). See
    # LeaveRequest's own docstring for how a balance is computed against this.
    doctor_annual_leave_days: Mapped[int]
    staff_annual_leave_days: Mapped[int]


class LeaveRequest(Base):
    """db/schema.sql's leave_requests table (migration 20260912065049) --
    the Leave Requests admin page's pending/approved/rejected review queue.
    identity_id, not staff_details_id, is the applicant FK -- an identity is
    the durable "who" a leave request belongs to, same reasoning
    StaffDetail.reports_to_id already points at identities rather than
    another staff_details row. Works uniformly for a doctor-role or
    receptionist-role login, since both are identities either way.

    decided_by/decided_at are both NULL until an admin approves or rejects
    this request, then both set together -- there is no "decided but by
    nobody" or "decided_by set but still pending" state. Duration is
    computed from from_date/to_date at read time, never stored."""
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    identity_id: Mapped[int] = mapped_column(ForeignKey("identities.id"))
    leave_type: Mapped[str]
    from_date: Mapped[str]
    to_date: Mapped[str]
    reason: Mapped[str | None]
    status: Mapped[str]
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("identities.id"))
    decided_at: Mapped[str | None]
    created_at: Mapped[str]


class PatientVisitNote(Base):
    """db/schema.sql's patient_visit_notes table (Section 12.10)."""
    __tablename__ = "patient_visit_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"))
    doctor_id: Mapped[str | None] = mapped_column(ForeignKey("doctors.id"))
    note_text: Mapped[str]
    created_at: Mapped[str]
    created_by_session_id: Mapped[str | None]


class PatientDocument(Base):
    """db/schema.sql's patient_documents table (Section 12.10)."""
    __tablename__ = "patient_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"))
    file_name: Mapped[str]
    file_url: Mapped[str]
    uploaded_at: Mapped[str]
    uploaded_by_session_id: Mapped[str | None]
    sent_to_whatsapp_at: Mapped[str | None]
    # Migration 0022 -- prescription/lab_report/diagnostic_report/other, see
    # db/migrations/versions/0022_patient_document_type.py.
    document_type: Mapped[str]


class PatientRow(Base):
    """db/schema.sql's patients table -- the full mapping (11 columns, matches
    the table exactly). Named PatientRow, not Patient, to leave that name
    free for a future dataclass -- see UserAccount's docstring for the same
    naming precedent. create_patient_profile()/link_existing_patient()/
    _link_patient_under_cap() (db/repositories/patients.py) do NOT use this
    model -- see those functions' own docstrings for why (advisory-lock +
    multi-statement-transaction code stays raw SQL permanently).
    patient_display_id (DCCP-<year>-<seq>) and mrn (MRN-<hospital short
    code>-<year>-<seq>) are generated together, same sequence number, by
    db/models.py's _generate_patient_identifiers() -- see migration 0003
    (original format) and db/display_ids.py (yearly-resetting format)."""
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    phone: Mapped[str]
    name: Mapped[str | None]
    date_of_birth: Mapped[str | None]
    gender: Mapped[str | None]
    address: Mapped[str | None]
    created_at: Mapped[str]
    status: Mapped[str]
    patient_display_id: Mapped[str | None]
    mrn: Mapped[str | None]
    # Possible-duplicate review flag (Section 0 follow-up): stamped once, at
    # creation, when this row matches another ACTIVE patient in this
    # hospital on at least 3 of {name, phone, date_of_birth, gender} -- see
    # db/repositories/patients.py's _flag_duplicate_if_matches(). Purely
    # informational for now (surfaced as a column on the portal's patient
    # list, no merge/dismiss action yet); duplicate_flag_reason is None
    # exactly when duplicate_of_patient_id is None.
    duplicate_of_patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    duplicate_flag_reason: Mapped[str | None]


class PatientLink(Base):
    """db/schema.sql's patient_links table -- the full mapping. See
    PatientRow's docstring for the same "advisory-lock code stays raw"
    caveat on the write path (_link_patient_under_cap). patient_id has no
    unique constraint here (unlike WhatsappIdentity.care_connect_account_id
    above) -- this is the genuine M:M join between whatsapp_phone and
    patients: one phone can hold up to MAX_ACTIVE_PATIENT_LINKS active
    links, and nothing stops the same patient being linked from a second
    phone. care_connect_account_id is NOT NULL (migration 0002) -- every
    write path (patients.py's _link_patient_under_cap(), init_db.py's
    _backfill_patient_links()) stamps it via _get_or_create_account_in_conn()
    at INSERT time; whatsapp_phone remains the actual join key, this column
    is not yet read by any lookup."""
    __tablename__ = "patient_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    whatsapp_phone: Mapped[str]
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    relationship_label: Mapped[str | None]
    linked_at: Mapped[str]
    unlinked_at: Mapped[str | None]
    service_consent: Mapped[bool]
    marketing_consent: Mapped[bool]
    care_connect_account_id: Mapped[int] = mapped_column(ForeignKey("care_connect_accounts.id"))


class DpdpConsent(Base):
    """db/schema.sql's dpdp_consents table -- one row per (hospital,
    whatsapp_phone) that has an on-file AGREED DPDP consent decision. See
    db/repositories/consent.py for the read/write logic; only the read side
    (has_agreed_to_dpdp_consent) is ORM-based so far -- record_dpdp_consent()
    is still raw SQL, see that function's own docstring for why."""
    __tablename__ = "dpdp_consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    whatsapp_phone: Mapped[str]
    care_connect_account_id: Mapped[int | None] = mapped_column(ForeignKey("care_connect_accounts.id"))
    consented_at: Mapped[str]


class AuditLog(Base):
    """db/schema.sql's audit_logs table -- two-level audit trail
    (tenant-capability-gating-plan.md's follow-up). actor_level is
    'platform_admin' or 'portal' (see db/repositories/audit_logs.py for the
    single source of truth on valid actions/redaction). hospital_id is
    nullable only for a hypothetical cross-tenant platform action; every row
    written today sets it."""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_level: Mapped[str]
    hospital_id: Mapped[int | None] = mapped_column(ForeignKey("hospitals.id"))
    actor_label: Mapped[str]
    action: Mapped[str]
    entity_type: Mapped[str | None]
    entity_id: Mapped[str | None]
    before_value: Mapped[str | None]
    after_value: Mapped[str | None]
    created_at: Mapped[str]


class CodeSequence(Base):
    """db/schema.sql's code_sequences table -- the single shared atomic
    counter every yearly-resetting display id (DCCG/DCCH/DCCC/DCCP, see
    db/display_ids.py's module docstring) increments through, keyed on
    (prefix, scope_key, period_key). scope_key is "global" for CareConnect
    account/hospital/clinic ids, or str(hospital_id) for patient ids;
    period_key is str(year) for all of them today. Deliberately NOT used by
    APT (appointment reference ids) -- that one predates this table and
    resets daily, not yearly, via its own reference_id_counters table."""
    __tablename__ = "code_sequences"

    id: Mapped[int] = mapped_column(primary_key=True)
    prefix: Mapped[str]
    scope_key: Mapped[str]
    period_key: Mapped[str]
    last_value: Mapped[int]


# StaffUser ("staff_users" table) used to live here -- unified per-person
# staff login (docs/rbac-redis-plan.md). Migration 0016 replaced it with
# Identity + StaffDetail; migration 0017 dropped the table. See Identity's
# own docstring.


class RolePermission(Base):
    """db/schema.sql's role_permissions table -- one row per (hospital, role,
    page), not a JSON blob, since portal/permissions.py reads this on every
    permission check and the Roles & Permissions admin UI edits it
    cell-by-cell."""
    __tablename__ = "role_permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    role: Mapped[str]
    page_key: Mapped[str]
    can_view: Mapped[bool]
    can_write: Mapped[bool]
    can_delete: Mapped[bool]


# SuperAdmin ("super_admins" table) used to live here -- global,
# not hospital-scoped platform-operator accounts. Migration 0016 replaced it
# with Identity + SuperAdminDetail; migration 0017 dropped the table. See
# Identity's own docstring.


class Identity(Base):
    """db/schema.sql's identities table (migration 0016) -- the shared
    identity/credential row for every principal in this app: a Google-OAuth
    hospital owner (google_id set, password_hash NULL), a password-login
    hospital staff member, or a password-login super admin (password_hash
    set for both, google_id NULL). email is globally unique
    (ux_identities_email, case-insensitive) across ALL of them.

    Deliberately carries NO privilege/role column of its own (no
    is_super_admin flag) -- which kind of principal a row is comes ONLY from
    whether a matching StaffDetail or SuperAdminDetail row exists. A flag on
    this shared, widely-written table would be one stray UPDATE (or a bug in
    some unrelated staff/OAuth write path) away from silently granting
    platform-wide access; a separate table means super-admin status
    requires an actual row, only ever inserted by the dedicated super-admin
    provisioning path (db/repositories/super_admins.py's create_super_admin)
    -- confirmed with the user, this safety property matters more than the
    extra table's minimal storage overhead. See migration 0016's own
    docstring for why this replaced the old users/staff_users/super_admins
    tables (and hospital_users), all four of which migration 0017 later
    dropped once these new tables were verified correct in production."""
    __tablename__ = "identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str]
    name: Mapped[str | None]
    google_id: Mapped[str | None]
    password_hash: Mapped[str | None]
    is_active: Mapped[bool]
    token_version: Mapped[int]
    created_at: Mapped[str]
    updated_at: Mapped[str | None]


class StaffDetail(Base):
    """db/schema.sql's staff_details table (migration 0016) -- the
    hospital/role/doctor_id extension for an Identity that's a hospital
    staff member, replacing StaffUser's own columns of the same name.
    identity_id is the primary key (1:1 with Identity, mirroring StaffUser's
    old "one staff_users row per person" cardinality). Since migration 0018,
    this also covers Google-OAuth hospital owners (role='admin' -- no
    separate 'owner' role; confirmed with the user, a hospital's role
    vocabulary stays exactly admin/receptionist/doctor) -- see the comment
    where HospitalOwner used to be defined, just below."""
    __tablename__ = "staff_details"

    identity_id: Mapped[int] = mapped_column(ForeignKey("identities.id"), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"))
    role: Mapped[str]
    doctor_id: Mapped[str | None] = mapped_column(ForeignKey("doctors.id"))
    # Migration 20260911174439 -- see schema.sql's own comment on this table
    # for why department_id is doctor-role-exclusive and attendance_status
    # has no history table behind it.
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"))
    phone: Mapped[str | None]
    address: Mapped[str | None]
    shift: Mapped[str | None]
    reports_to_id: Mapped[int | None] = mapped_column(ForeignKey("identities.id"))
    attendance_status: Mapped[str]


class SuperAdminDetail(Base):
    """db/schema.sql's super_admin_details table (migration 0016) -- a bare
    marker row: an Identity with a matching row here is a platform/super
    admin. Deliberately a separate table, not a flag on Identity itself --
    see Identity's own docstring for the security reasoning (privilege
    requires a row only the dedicated provisioning path ever inserts, not a
    column any Identity-writing code could accidentally flip)."""
    __tablename__ = "super_admin_details"

    identity_id: Mapped[int] = mapped_column(ForeignKey("identities.id"), primary_key=True)


# HospitalOwner ("hospital_owners" table) used to live here -- migration
# 0016 introduced it as an M:M ownership link (Identity <-> Hospital) for
# Google-OAuth sign-ins, repointed from the old HospitalUser table.
# Migration 0018 folded it into StaffDetail: confirmed with the user, no
# identity actually owns more than one hospital in practice, so the M:M
# shape was pure redundancy with StaffDetail's own (identity_id, hospital_id,
# role) columns. A hospital owner is now just a StaffDetail row with
# role='admin' -- same table, same login path (portal/routes/staff_auth.py)
# as every other staff member. See StaffDetail's own docstring.


class PlatformSettings(Base):
    """db/schema.sql's platform_settings table -- a SINGLETON row (id is
    always 1, enforced by a CHECK constraint, not per-hospital) holding
    cross-tenant values only a platform/super admin can change, as opposed
    to hospitals' own self-serve settings (business_hours_text,
    session_timeout_minutes, ...). max_active_patient_links starts as
    db/models.py's former MAX_ACTIVE_PATIENT_LINKS=5 hardcoded constant --
    see db/repositories/platform_settings.py for the read/update API and
    admin/platform_settings_api.py for the TENANTS_ADMIN_SECRET-gated
    endpoint that edits it."""
    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    max_active_patient_links: Mapped[int]
    # Migration 0014: moved off hospitals.feature_labels/dpdp_consent_required
    # -- ONE value for every tenant now, not a per-hospital override. Stored
    # as JSON text, same convention as HospitalRow.feature_labels.
    feature_labels: Mapped[str | None]
    dpdp_consent_required: Mapped[bool]


class HospitalSettings(Base):
    """db/schema.sql's hospital_settings table (migration 0021) -- a
    PER-HOSPITAL counterpart to platform_settings above: exactly one row per
    hospital (hospital_id is both the primary key and the FK, 1:1), holding
    self-serve settings that don't belong as more columns on the already very
    wide `hospitals` table (confirmed with the user). Row is created lazily,
    on first read or write (db/repositories/hospital_settings.py's
    get_hospital_settings()), not at hospital-creation time -- so this is
    safe to introduce without touching every hospital-creation code path
    (create_hospital(), db/seed.py, admin onboarding)."""
    __tablename__ = "hospital_settings"

    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), primary_key=True)
    # Follow-up eligibility window: an ATTENDED appointment stops being
    # eligible for Follow-up this many days after its own scheduled_at. NULL
    # means "use the 30-day code-level default" (db/repositories/
    # appointments.py's DEFAULT_FOLLOWUP_VALIDITY_DAYS).
    followup_validity_days: Mapped[int | None]
    # Fee lines shown on Follow-up's confirm/success cards (followup_fee) and
    # reserved for New Consultation's cards once that's wired up
    # (new_consultation_fee, stored but not displayed yet -- confirmed with
    # the user). NULL means "no fee configured," which omits the fee line
    # entirely rather than showing a fake ₹0.
    followup_fee: Mapped[float | None] = mapped_column(Numeric(10, 2))
    new_consultation_fee: Mapped[float | None] = mapped_column(Numeric(10, 2))
    # Lab Test Phase 2 follow-up: flat fee added to a home-collection Lab
    # Test booking's price review, same "unset omits the line" convention as
    # the two fees above.
    home_collection_charge: Mapped[float | None] = mapped_column(Numeric(10, 2))
    # Slot-generation window (migration 0031): how many days ahead a
    # doctor's grid is computed live (db/repositories/doctors.py's
    # compute_doctor_candidate_slots(), migration 0032) and a diagnostic/
    # procedure resource's rolling window is extended
    # (generate_slots_for_resource()/_procedure_resource()). NULL means "use
    # the code-level DEFAULT_FUTURE_BOOKING_DAYS default" (db/repositories/
    # hospital_settings.py), same convention as followup_validity_days above.
    future_booking_days: Mapped[int | None]


class GoogleCalendarConnection(Base):
    """db/schema.sql's google_calendar_connections table (migration 0025,
    repointed from doctor_id to hospital_id by migration 0029) -- Google
    Meet integration, alongside (not replacing) the existing Jitsi
    tele-consultation link. One row per HOSPITAL (hospital_id is both the
    primary key and the FK, 1:1), connected once by a hospital admin and
    used to create every doctor's tele-consultation Meet links -- not one
    row per doctor (confirmed with the user: individual per-doctor consent
    was the wrong shape for how hospitals actually want to adopt this). A
    row's mere EXISTENCE is what "this hospital is connected" means (unlike
    HospitalSettings above, no row is ever created lazily/blank;
    db/repositories/google_calendar.py only ever inserts one once the OAuth
    dance in auth/google_calendar_oauth.py actually succeeds).

    access_token/refresh_token are Fernet-encrypted (core/crypto.py) before
    they ever reach this table -- stored as opaque TEXT, same "everything
    opaque is TEXT" convention as every other blob in this file, never the
    raw token. access_token_expires_at is ISO-8601 TEXT like every other
    timestamp column in this file, not DateTime (see the module docstring)."""
    __tablename__ = "google_calendar_connections"

    hospital_id: Mapped[int] = mapped_column(ForeignKey("hospitals.id"), primary_key=True)
    # The connected Google account's own email -- display-only (the
    # "Connected as ___" line on the hospital admin's Settings page), never
    # used to look anything up.
    google_email: Mapped[str | None]
    encrypted_access_token: Mapped[str]
    encrypted_refresh_token: Mapped[str]
    access_token_expires_at: Mapped[str]
    # Which Google calendar (of the connected account's own) new Meet events
    # are created on -- "primary" for every connection today (no calendar
    # picker built yet), kept as its own column so that UI can be added
    # later without a schema change.
    calendar_id: Mapped[str]
    connected_at: Mapped[str]
    updated_at: Mapped[str]
