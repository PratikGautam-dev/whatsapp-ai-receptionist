
"""
Creates the schema (SPEC Section 4 -- see db/migrations/versions/
0001_baseline_schema.py's frozen SQL, or db/schema.sql for the same content
kept as a human-readable, no-longer-executed reference) and seeds the default
hospital (db/seed.py). Safe to re-run — every CREATE is IF NOT EXISTS and
seed_default_hospital() no-ops if that hospital already exists.

Run directly to set up (or update) the on-disk database:
    python -m db.init_db
core/main.py also calls init_db() once at startup, so a fresh clone works
without a manual step.
"""
import importlib
import re
from pathlib import Path

from alembic import command
from alembic.config import Config

from core.config import get_settings
from db import seed
from db.connection import get_connection, get_database_url
from db.display_ids import CARE_CONNECT_ACCOUNT_PREFIX, GLOBAL_SCOPE_KEY, generate_yearly_display_id_conn
from db.repositories.accounts import _get_or_create_account_in_conn
from db.repositories.appointment_types import DEFAULT_APPOINTMENT_TYPES, default_is_active
from db.repositories.diagnostic_tests import CATEGORY_DIAGNOSTIC, CATEGORY_LAB, DEFAULT_DIAGNOSTIC_TESTS, DEFAULT_LAB_TESTS
from db.models import DEFAULT_MAX_ACTIVE_PATIENT_LINKS
from db.repository import _generate_patient_identifiers, _get_or_create_hospital_short_code

# SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"

# db/schema.sql is retired as a runtime dependency -- kept on disk purely as
# a human-readable reference (SPEC Section 4's own schema documentation).
# db/migrations/versions/0001_baseline_schema.py's _BASELINE_SQL is a frozen,
# byte-for-byte snapshot of it taken at the moment Alembic was adopted, and is
# now the actual source this module applies -- importlib, not a normal
# `import`, because "0001_baseline_schema" isn't a valid Python identifier.
_baseline_schema = importlib.import_module("db.migrations.versions.0001_baseline_schema")


def run_alembic_migrations() -> None:
    """Establishes schema version tracking (alembic_version table) going
    forward -- production/dev only, deliberately NOT called by
    init_db_on_connection() below, so the per-test fixture in
    tests/conftest.py (which calls init_db_on_connection() directly, hundreds
    of times per run) is completely unaffected by this addition.

    Safe to run every time this process starts, including against a database
    the baseline schema is already fully applied to: migration 0001
    (db/migrations/versions/0001_baseline_schema.py) is entirely IF NOT
    EXISTS/IF EXISTS-guarded, so replaying it is a verified no-op.
    init_db_on_connection() below still separately applies that same frozen
    baseline SQL too, for the same reason -- redundant but harmless."""
    alembic_cfg = Config(str(_ALEMBIC_INI_PATH))
    # alembic.ini's script_location is the relative path "db/migrations",
    # which Alembic resolves against the process's CWD, not the ini file's
    # own directory -- overridden here to an absolute path so this works
    # regardless of where this process was started from (e.g. Docker's
    # CMD/WORKDIR, or `python -m db.init_db` run from an unexpected cwd).
    alembic_cfg.set_main_option("script_location", str(_ALEMBIC_INI_PATH.parent / "db" / "migrations"))
    command.upgrade(alembic_cfg, "head")


def _backfill_enabled_features(conn) -> None:
    """SPEC Section 14.5: one-time, idempotent backfill for any hospital row
    with enabled_features still NULL (created before this column existed, or
    seeded by seed.py's explicit column lists, which don't set it directly).
    Only ever touches NULL rows, so re-running this on every startup is safe
    and never overwrites a real tenant's own later choices. 'booking' rows
    get EXACTLY the old flow_type='booking' main menu, item for item: Book
    Appointment, Reschedule, Cancel, and the static "FAQ" button (which just
    sent hospital-info text -- now the "hospital_info" feature) -- true
    behavior parity, no new capability silently switched on for an existing
    tenant. 'faq' rows get just ["faq"]; anything else (a flow_type this
    migration doesn't recognize) gets an empty set rather than guessing."""
    conn.execute(
        "UPDATE hospitals SET enabled_features = ? "
        "WHERE enabled_features IS NULL AND flow_type = 'booking'",
        ('["booking","reschedule","cancel","hospital_info"]',),
    )
    conn.execute(
        "UPDATE hospitals SET enabled_features = ? WHERE enabled_features IS NULL AND flow_type = 'faq'",
        ('["faq"]',),
    )
    conn.execute("UPDATE hospitals SET enabled_features = '[]' WHERE enabled_features IS NULL")
    conn.commit()


def _backfill_patients(conn) -> None:
    """SPEC Section 12.9: patients was always in Section 4's original data
    model but never actually built until staff-side patient SEARCH (by name,
    not just phone) needed somewhere to store one. Every distinct
    (hospital_id, phone) pair already sitting in appointments gets a row here
    (name NULL -- WhatsApp bookings never collected one) so existing patients
    are searchable by phone immediately, without waiting for their next
    booking to (re-)upsert them.

    Patient identity SEPARATION (Spec.md Section 0) dropped patients'
    UNIQUE(hospital_id, phone) constraint (multiple profiles per phone are
    now allowed), so the ON CONFLICT (hospital_id, phone) DO NOTHING this
    used to rely on no longer has a matching unique index to target --
    switched to the same WHERE NOT EXISTS idiom every other backfill in this
    file already uses. Behaviorally identical: still only ever ADDS a row
    for a (hospital_id, phone) with zero existing patients rows, never
    touches one that already exists (so it can never clobber a name staff
    already filled in, and never creates a 2nd row for a phone this backfill
    already covered on an earlier startup)."""
    conn.execute(
        "INSERT INTO patients (hospital_id, phone) "
        "SELECT DISTINCT a.hospital_id, a.phone FROM appointments a "
        "WHERE NOT EXISTS (SELECT 1 FROM patients p WHERE p.hospital_id = a.hospital_id AND p.phone = a.phone)"
    )
    conn.commit()


def _backfill_appointment_patient_denorm(conn) -> None:
    """Item 8 (Spec.md Section 0): appointments.patient_id/patient_name/
    patient_phone are populated going forward by create_appointment() itself
    -- this is the one-time catch-up for every row that predates those
    columns. Only ever touches rows where patient_id IS NULL, so re-running
    on every startup is a safe no-op once caught up, and it can never
    overwrite a value create_appointment() already set correctly."""
    conn.execute(
        "UPDATE appointments a SET patient_id = p.id, patient_name = p.name, patient_phone = a.phone "
        "FROM patients p WHERE p.hospital_id = a.hospital_id AND p.phone = a.phone AND a.patient_id IS NULL"
    )
    conn.commit()


def _backfill_appointment_patient_date_of_birth(conn) -> None:
    """Family/multi-person-booking follow-up (Spec.md Section 0):
    appointments.patient_date_of_birth is populated going forward by
    create_appointment() itself -- catches up every row that predates that
    column, from the (single, mutable) patients.date_of_birth value, same
    best-effort approximation the column's own docstring already flags
    (originally patient_age/patients.age, replaced by the DOB migration --
    see that migration's own docstring). Gated on a.patient_date_of_birth
    IS NULL specifically (not reusing the patient_id gate above) since a
    row can already have patient_id/patient_name set from an earlier
    startup's run of the backfill above, before this column existed --
    that gate alone would skip it here."""
    conn.execute(
        "UPDATE appointments a SET patient_date_of_birth = p.date_of_birth "
        "FROM patients p WHERE p.hospital_id = a.hospital_id AND p.phone = a.phone AND a.patient_date_of_birth IS NULL"
    )
    conn.commit()


def _backfill_patient_display_ids(conn) -> None:
    """Patient identity system (Spec.md Section 0): every patient created
    before this feature existed has patient_display_id (and mrn) still
    NULL -- assigned here, in CREATION ORDER per hospital (created_at, then
    id as a tiebreak for same-instant rows), via the exact same
    _generate_patient_identifiers() (short-code derivation + the
    patient_id_counters atomic sequence) that create_appointment()'s
    _upsert_patient() uses for every NEW patient going forward -- so the
    counter this backfill leaves behind is exactly where the next real
    patient's id continues from, no gap or collision. Only ever touches
    patient_display_id IS NULL rows, so re-running this on every startup is
    a safe no-op once every hospital is caught up (each hospital's counter
    only advances while there's still a NULL row left for it). See
    _backfill_patient_mrns() below for the separate case of a patient that
    already has a patient_display_id but predates the mrn column."""
    hospital_ids = [
        row["hospital_id"] for row in conn.execute(
            "SELECT DISTINCT hospital_id FROM patients WHERE patient_display_id IS NULL ORDER BY hospital_id"
        ).fetchall()
    ]
    for hospital_id in hospital_ids:
        patient_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM patients WHERE hospital_id = ? AND patient_display_id IS NULL "
                "ORDER BY created_at, id",
                (hospital_id,),
            ).fetchall()
        ]
        for patient_id in patient_ids:
            display_id, mrn = _generate_patient_identifiers(conn, hospital_id)
            conn.execute(
                "UPDATE patients SET patient_display_id = ?, mrn = ? WHERE id = ?", (display_id, mrn, patient_id),
            )
    conn.commit()


def _backfill_patient_link_accounts(conn) -> None:
    """CareConnect account/identity layer (db/schema.sql's own comment on
    care_connect_accounts/whatsapp_identities): catches any patient_links
    row still missing a care_connect_account_id -- either a genuinely
    legacy row (from before this layer existed) or one from a startup that
    ran between this feature's rollout and this backfill catching up.
    _link_patient_under_cap() (db/repositories/patients.py) stamps every
    NEW row inline now, so this is a safety net, not the main write path --
    but it MUST run before the ALTER ... SET NOT NULL below, or that
    statement fails outright on any database with a row this hasn't caught
    yet (see migration 0002's own docstring for how that broke in prod).

    Deliberately collapses onto ONE shared account per distinct
    whatsapp_phone across ALL hospitals (not one per hospital) -- the
    account layer is global by design. provider_user_id is set to the
    phone itself -- the practical fallback since historical rows never
    captured a real WhatsApp wa_id distinct from the phone string. Gated on
    care_connect_account_id IS NULL, so re-running this on every startup is
    a safe no-op once every phone is caught up."""
    phones = [
        row["whatsapp_phone"] for row in conn.execute(
            "SELECT DISTINCT whatsapp_phone FROM patient_links WHERE care_connect_account_id IS NULL"
        ).fetchall()
    ]
    for phone in phones:
        account_row = conn.execute(
            "SELECT care_connect_account_id FROM whatsapp_identities WHERE provider_user_id = ?", (phone,),
        ).fetchone()
        if account_row is not None:
            account_id = account_row["care_connect_account_id"]
        else:
            new_account = conn.execute("INSERT INTO care_connect_accounts DEFAULT VALUES RETURNING id").fetchone()
            account_id = new_account["id"]
            conn.execute(
                "UPDATE care_connect_accounts SET display_id = ? WHERE id = ?",
                (generate_yearly_display_id_conn(conn, CARE_CONNECT_ACCOUNT_PREFIX, GLOBAL_SCOPE_KEY), account_id),
            )
            conn.execute(
                "INSERT INTO whatsapp_identities (care_connect_account_id, provider_user_id, phone_number) "
                "VALUES (?, ?, ?)",
                (account_id, phone, phone),
            )
        conn.execute(
            "UPDATE patient_links SET care_connect_account_id = ? "
            "WHERE whatsapp_phone = ? AND care_connect_account_id IS NULL",
            (account_id, phone),
        )
    conn.commit()


def _backfill_patient_mrns(conn) -> None:
    """mrn is a NEWER column than patient_display_id -- a patient that
    already got a patient_display_id before mrn existed needs an mrn too,
    but WITHOUT consuming a fresh patient_id_counters number (that would
    break the "same sequence number" pairing _generate_patient_identifiers()
    guarantees for every patient created after this column existed).
    Instead, reuses the number already embedded in that patient's own
    patient_display_id suffix (e.g. ...-0007 -> MRN-<code>-0007). Only
    touches mrn IS NULL rows, so re-running this on every startup is a
    no-op once every hospital is caught up."""
    rows = conn.execute(
        "SELECT id, hospital_id, patient_display_id FROM patients "
        "WHERE mrn IS NULL AND patient_display_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        code = _get_or_create_hospital_short_code(conn, row["hospital_id"])
        seq_part = row["patient_display_id"].rsplit("-", 1)[1]
        mrn = f"MRN-{code}-{seq_part}"
        conn.execute("UPDATE patients SET mrn = ? WHERE id = ?", (mrn, row["id"]))
    conn.commit()


def _backfill_patient_links(conn) -> None:
    """Patient identity SEPARATION (Spec.md Section 0): every `patients` row
    that predates this feature (i.e. every one that currently exists, since
    this migration ships alongside the feature itself) gets exactly one
    patient_links row, relationship_label='Self' -- the implicit "this is the
    phone's own profile" link every pre-existing patient already had, just
    never modeled explicitly. `linked_at` is backdated to the patient's own
    `created_at` rather than "now" -- preserves the real historical ordering
    get_active_patients_for_phone() sorts by, in case a phone somehow already
    had more than one `patients` row before this ran (shouldn't happen given
    the old UNIQUE(hospital_id, phone) constraint, but this keeps the
    backfill correct even in that edge case rather than assuming it away).

    care_connect_account_id is resolved/created inline via the same raw-conn
    helper _link_patient_under_cap() uses (patients.py) -- patient_links.
    care_connect_account_id is NOT NULL, so the row has to be stamped with a
    real account at INSERT time; there's no later pass that goes back and
    fills it in. Deliberately collapses onto ONE shared account per distinct
    whatsapp_phone across ALL hospitals (not one per hospital), matching
    get_or_create_account()'s own global-identity resolution.

    Gated on NOT EXISTS (one link per patient, not per phone -- a patient
    with two links from a bad prior run would break this gate, but nothing
    in this codebase ever creates more than one link per patient), so
    re-running this on every startup is a safe no-op once every existing
    patient is linked. Does not touch `patients` or `appointments` at all --
    purely additive rows in the new table, zero risk to existing booking
    history or Patient IDs."""
    rows = conn.execute(
        "SELECT p.hospital_id, p.phone, p.id AS patient_id, p.created_at FROM patients p "
        "WHERE NOT EXISTS (SELECT 1 FROM patient_links pl WHERE pl.patient_id = p.id)"
    ).fetchall()
    for row in rows:
        account = _get_or_create_account_in_conn(conn, row["phone"], phone_number=row["phone"])
        conn.execute(
            "INSERT INTO patient_links "
            "(hospital_id, whatsapp_phone, patient_id, relationship_label, linked_at, care_connect_account_id) "
            "VALUES (?, ?, ?, 'Self', ?, ?)",
            (row["hospital_id"], row["phone"], row["patient_id"], row["created_at"], account["id"]),
        )
    conn.commit()


def _backfill_appointment_types(conn) -> None:
    """Appointment type step (WhatsApp flow alignment): seeds
    DEFAULT_APPOINTMENT_TYPES (db/repositories/appointment_types.py, the
    single source of truth this mirrors) for every hospital that doesn't
    already have a row for a given type id -- so a hospital created before
    this feature, and a hospital onboarded after it, both end up with the
    same fixed catalog, ready to relabel/deactivate via the portal without
    a second migration. Gated per (hospital_id, type id), so re-running this
    on every startup is a safe no-op once every hospital is caught up, and a
    hospital that's already relabeled/deactivated a type is never touched
    again. is_active for a newly-inserted row is resolved from the hospital's
    own tenant_type (default_is_active(), appointment_types.py's single
    source of truth), so a pre-existing hospital gets the same
    tenant-appropriate default a freshly-onboarded one would."""
    hospitals = conn.execute("SELECT id, tenant_type FROM hospitals").fetchall()
    for hospital in hospitals:
        hospital_id, tenant_type = hospital["id"], hospital["tenant_type"]
        for sort_order, appt_type in enumerate(DEFAULT_APPOINTMENT_TYPES):
            conn.execute(
                "INSERT INTO appointment_types "
                "(id, hospital_id, label, requires_consent, requires_doctor_selection, is_active, sort_order) "
                "SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM appointment_types WHERE hospital_id = ? AND id = ?)",
                (
                    appt_type["id"], hospital_id, appt_type["label"], appt_type["requires_consent"],
                    appt_type["requires_doctor_selection"], default_is_active(tenant_type, appt_type["id"]),
                    sort_order, hospital_id, appt_type["id"],
                ),
            )
    conn.commit()


_DEFAULT_PROCEDURES = (
    # (category, name, booking_mode, duration_minutes, required_resource_types)
    ("injection", "Injection", "instant", 15, ("staff",)),
    ("dressing_wound_care", "Dressing / Wound Care", "instant", 20, ("staff",)),
    ("chemotherapy", "Chemotherapy", "approval_required", 180, ("bed_chair", "equipment", "staff")),
)


def _backfill_procedures(conn) -> None:
    """Daycare/Procedure rebuild: seeds a small default catalog for every
    hospital that doesn't already have ANY `procedures` row -- same "has-any"
    gating _backfill_diagnostic_tests uses (an open, hospital-editable
    catalog, not a fixed one re-keyed by id). No procedure_resources pool is
    seeded (same as diagnostic_resources) -- a hospital links real bed/chair/
    equipment/staff resources via the portal afterward; until then these
    procedures show "not available right now", same as an unconfigured
    diagnostic test."""
    hospitals = conn.execute("SELECT id FROM hospitals").fetchall()
    for hospital in hospitals:
        hospital_id = hospital["id"]
        has_any = conn.execute(
            "SELECT 1 FROM procedures WHERE hospital_id = ? LIMIT 1", (hospital_id,),
        ).fetchone()
        if has_any:
            continue
        for sort_order, (category, name, booking_mode, duration_minutes, resource_types) in enumerate(_DEFAULT_PROCEDURES):
            row = conn.execute(
                "INSERT INTO procedures (hospital_id, category, name, booking_mode, duration_minutes, is_active, sort_order) "
                "VALUES (?, ?, ?, ?, ?, TRUE, ?) RETURNING id",
                (hospital_id, category, name, booking_mode, duration_minutes, sort_order),
            ).fetchone()
            for resource_type in resource_types:
                conn.execute(
                    "INSERT INTO procedure_required_resource_types (hospital_id, procedure_id, resource_type) "
                    "VALUES (?, ?, ?)",
                    (hospital_id, row["id"], resource_type),
                )
    conn.commit()


def _backfill_diagnostic_tests(conn) -> None:
    """Diagnostic/Lab Phase 2 (docs/per-appointment-type-flow-plan.md Step
    5): seeds DEFAULT_DIAGNOSTIC_TESTS/DEFAULT_LAB_TESTS for every hospital
    that doesn't already have ANY row in either category -- same "has-any"
    gating as _backfill_daycare_duration_options above (an open, hospital-
    editable catalog, not a fixed one re-keyed by id). Each seeded test has
    no schedule/price configured yet -- hospitals set those up afterward via
    the portal."""
    hospitals = conn.execute("SELECT id FROM hospitals").fetchall()
    for hospital in hospitals:
        hospital_id = hospital["id"]
        for category, names in ((CATEGORY_DIAGNOSTIC, DEFAULT_DIAGNOSTIC_TESTS), (CATEGORY_LAB, DEFAULT_LAB_TESTS)):
            has_any = conn.execute(
                "SELECT 1 FROM diagnostic_tests WHERE hospital_id = ? AND category = ? LIMIT 1",
                (hospital_id, category),
            ).fetchone()
            if has_any:
                continue
            for sort_order, name in enumerate(names):
                conn.execute(
                    "INSERT INTO diagnostic_tests (hospital_id, category, name, is_active, sort_order) "
                    "VALUES (?, ?, ?, TRUE, ?)",
                    (hospital_id, category, name, sort_order),
                )
    conn.commit()


def _backfill_reports_prescriptions_feature(conn) -> None:
    """CareConnect architecture doc alignment (Spec.md Section 0): "my_details"
    renamed to "reports_prescriptions" (Section 20's exact menu item) --
    the underlying implementation is unchanged, just the feature KEY.
    Plain string REPLACE on the raw JSON-encoded TEXT columns (both
    enabled_features, a JSON array member, and feature_labels, a JSON
    object key) is correct here specifically because "my_details" only
    ever appears as a full quoted JSON string/key, never as a substring of
    anything else -- and is naturally idempotent, since the WHERE clause
    finds nothing left to touch on a second run once every hospital's
    already been converted."""
    #  (not %) -- db/connection.py's execute() always passes a params
    # tuple to psycopg2, which treats a bare % in the query as the start of
    # its own substitution syntax even with nothing to substitute.
    conn.execute(
        "UPDATE hospitals SET enabled_features = REPLACE(enabled_features, '\"my_details\"', '\"reports_prescriptions\"') "
        "WHERE enabled_features LIKE '%%my_details%%'"
    )
    conn.execute(
        "UPDATE hospitals SET feature_labels = REPLACE(feature_labels, '\"my_details\"', '\"reports_prescriptions\"') "
        "WHERE feature_labels LIKE '%%my_details%%'"
    )
    conn.commit()


def _backfill_book_doctor_tests_diagnostics_split(conn) -> None:
    """WhatsApp menu restructuring: the single "booking" feature (one
    main-menu row, one flat 7-type list) split into two
    independently-toggleable features -- "book_doctor_appointment"
    (new/followup/tele) and "tests_diagnostics" (diagnostic/lab/daycare) --
    each with its own category submenu. Every hospital that had "booking" on
    must end up with BOTH new keys: this is a mandatory rename+expand of a
    capability hospitals already had enabled, unlike consent_privacy/
    manage_language (new-hospital-only, no backfill needed).

    Same "plain string REPLACE on the raw JSON-encoded TEXT column"
    technique _backfill_reports_prescriptions_feature above uses --
    "booking" only ever appears as a full quoted JSON array member/object key
    here, never a substring of another feature key. Two steps since this is
    a 1->2 expansion, not a plain rename: first rename booking ->
    book_doctor_appointment in place, then, for any row that now has
    book_doctor_appointment but not yet tests_diagnostics, append
    tests_diagnostics before the closing bracket. Both steps (and the
    feature_labels rename, carrying a hospital's own custom "booking" label
    forward onto book_doctor_appointment only -- there's no correct 1:1
    mapping for the new second row, so it just falls back to the default
    translation) are naturally idempotent, same as every other one-time
    JSON-literal backfill in this file."""
    conn.execute(
        "UPDATE hospitals SET enabled_features = "
        "REPLACE(enabled_features, '\"booking\"', '\"book_doctor_appointment\"') "
        "WHERE enabled_features LIKE '%%booking%%'"
    )
    conn.execute(
        "UPDATE hospitals SET enabled_features = "
        "REPLACE(enabled_features, ']', ',\"tests_diagnostics\"]') "
        "WHERE enabled_features LIKE '%%book_doctor_appointment%%' "
        "AND enabled_features NOT LIKE '%%tests_diagnostics%%'"
    )
    conn.execute(
        "UPDATE hospitals SET feature_labels = "
        "REPLACE(feature_labels, '\"booking\"', '\"book_doctor_appointment\"') "
        "WHERE feature_labels LIKE '%%booking%%'"
    )
    conn.commit()


def _backfill_admin_capabilities(conn) -> None:
    """Tenant-type-driven capability gating (tenant-capability-gating-plan.md):
    every existing hospital row gets an EXPLICIT admin_capabilities value
    (rather than leaving it NULL and relying on
    backend/portal/capabilities.py's get_capabilities() runtime fallback) so
    nothing silently depends on "default = full access" -- same "backfill
    explicitly, don't just infer at read time" precedent
    _backfill_enabled_features() above already established. Only touches
    rows where admin_capabilities IS NULL, so re-running this on every
    startup is a safe no-op once every hospital is caught up, and it can
    never overwrite a value a tenant admin already edited via
    admin/tenants_api.py. The literal JSON arrays here are a snapshot
    matching backend/portal/capabilities.py's DEFAULT_CAPABILITIES_BY_TYPE
    at the time this migration was written -- that module (not this
    function) is the single source of truth application code actually
    reads through; this only needs to match at backfill time, same as
    every other one-time JSON-literal backfill in this file.

    The final catch-all (admin_capabilities = '[]' for anything still NULL
    after the two typed UPDATEs above) covers a tenant_type value other than
    'hospital'/'clinic' -- shouldn't happen given the column's own CHECK
    constraint, but a zero-capability default is the safe failure mode if it
    ever does, rather than leaving the column NULL (which would fall back to
    the full hospital capability set via get_capabilities())."""
    conn.execute(
        "UPDATE hospitals SET admin_capabilities = ? "
        "WHERE admin_capabilities IS NULL AND tenant_type = 'hospital'",
        ('["manage_doctors","manage_departments","manage_appointment_types",'
         '"manage_bookings","manage_settings","manage_staff"]',),
    )
    conn.execute(
        "UPDATE hospitals SET admin_capabilities = ? "
        "WHERE admin_capabilities IS NULL AND tenant_type = 'clinic'",
        ('["manage_bookings","manage_settings"]',),
    )
    conn.execute("UPDATE hospitals SET admin_capabilities = '[]' WHERE admin_capabilities IS NULL")
    conn.commit()


def _backfill_diagnostic_resources_capability(conn) -> None:
    """Diagnostic/Lab Phase 2: manage_diagnostic_resources is a capability
    added AFTER _backfill_admin_capabilities() above first ran for every
    hospital -- that function only ever fills a NULL admin_capabilities, so
    an already-backfilled hospital would never pick up a capability added to
    DEFAULT_CAPABILITIES_BY_TYPE afterward without this. Same "append into
    the raw JSON array" technique _backfill_book_doctor_tests_diagnostics_
    split() uses for enabled_features, hospital-tier only (clinics don't get
    this by default, same as manage_doctors/manage_departments). Excludes
    '[]' explicitly -- REPLACE would otherwise produce invalid JSON
    (a leading comma) for that one degenerate value."""
    conn.execute(
        "UPDATE hospitals SET admin_capabilities = "
        "REPLACE(admin_capabilities, ']', ',\"manage_diagnostic_resources\"]') "
        "WHERE tenant_type = 'hospital' AND admin_capabilities IS NOT NULL "
        "AND admin_capabilities != '[]' "
        "AND admin_capabilities NOT LIKE '%%manage_diagnostic_resources%%'"
    )
    conn.commit()


def _backfill_procedures_capability(conn) -> None:
    """Daycare/Procedure rebuild: same "append into the raw JSON array"
    backfill _backfill_diagnostic_resources_capability above uses, for the
    new manage_procedures capability."""
    conn.execute(
        "UPDATE hospitals SET admin_capabilities = "
        "REPLACE(admin_capabilities, ']', ',\"manage_procedures\"]') "
        "WHERE tenant_type = 'hospital' AND admin_capabilities IS NOT NULL "
        "AND admin_capabilities != '[]' "
        "AND admin_capabilities NOT LIKE '%%manage_procedures%%'"
    )
    conn.commit()


def _backfill_handoff_messages(conn) -> None:
    """Handoff two-way threading follow-up (Spec.md Section 0): every
    pre-existing handoff_requests row's own message_text becomes that
    thread's first inbound handoff_messages row, so get_handoff_messages()
    (the portal's single source of truth for the chat thread) isn't empty
    for a handoff that predates this table. Only ever touches a
    handoff_requests row with zero handoff_messages rows yet, so this is a
    safe no-op to re-run on every startup once caught up."""
    conn.execute(
        "INSERT INTO handoff_messages (hospital_id, handoff_request_id, direction, message_text, created_at) "
        "SELECT hr.hospital_id, hr.id, 'inbound', hr.message_text, hr.created_at "
        "FROM handoff_requests hr "
        "WHERE hr.message_text IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM handoff_messages hm WHERE hm.handoff_request_id = hr.id)"
    )
    conn.commit()


def init_db_on_connection(conn) -> int:
    """Apply schema + seed data to an already-open connection. Used directly by
    tests (against an in-memory DB) and internally by init_db() below.

    run_alembic_migrations() is deliberately NOT called here (see its own
    docstring) -- the per-test fixture in tests/conftest.py calls this
    function directly, hundreds of times per run, and never runs Alembic at
    all. So every schema DELTA added after the 0001 baseline (migrations
    0002+) needs its own idempotent statement here too, same
    "ALTER TABLE ... ADD COLUMN IF NOT EXISTS" pattern schema.sql used for
    years before Alembic adoption -- redundant against a DB that already
    got there via `alembic upgrade head` (prod), but the only way it
    reaches a test's freshly-recreated schema at all."""
    # schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    # conn.executescript(schema_sql)
    conn.executescript(_baseline_schema._BASELINE_SQL)
    # Migration 0002: patient_links.care_connect_account_id NOT NULL. Must
    # backfill any legacy NULL row FIRST -- this ALTER runs unconditionally
    # on every startup (prod included), and Postgres refuses to add a NOT
    # NULL constraint while a NULL value still exists, which would crash
    # init_db() (and the whole app, since main.py calls it at import time)
    # on any database old enough to have a row that predates the
    # CareConnect account layer.
    _backfill_patient_link_accounts(conn)
    conn.execute("ALTER TABLE patient_links ALTER COLUMN care_connect_account_id SET NOT NULL")
    # Migration 0003: patients.mrn.
    conn.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS mrn TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_patients_hospital_mrn "
        "ON patients(hospital_id, mrn) WHERE mrn IS NOT NULL"
    )
    # Migration 0004: appointments.video_link (tele-consultation Jitsi URL --
    # was in schema.sql but never made it into the frozen 0001 baseline or
    # any numbered migration, so it silently didn't exist anywhere that
    # only ever ran `alembic upgrade head`).
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS video_link TEXT")
    # Migration 0005: partial index supporting _link_patient_under_cap()'s
    # (db/repositories/patients.py) account-scoped active-link count.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_patient_links_active_account "
        "ON patient_links(hospital_id, care_connect_account_id) WHERE unlinked_at IS NULL"
    )
    # Migration 0006: care_connect_accounts.display_id / hospitals.display_id
    # (db/display_ids.py). Must run BEFORE seed_default_hospital() below --
    # that function now stamps display_id on the row it creates.
    conn.execute("ALTER TABLE care_connect_accounts ADD COLUMN IF NOT EXISTS display_id TEXT")
    conn.execute("ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS display_id TEXT")
    conn.execute("UPDATE care_connect_accounts SET display_id = 'DCC-ACC-' || lpad(id::text, 6, '0') WHERE display_id IS NULL")
    conn.execute("UPDATE hospitals SET display_id = 'DCC-HOS-' || lpad(id::text, 4, '0') WHERE display_id IS NULL")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_care_connect_accounts_display_id "
        "ON care_connect_accounts(display_id) WHERE display_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_hospitals_display_id "
        "ON hospitals(display_id) WHERE display_id IS NOT NULL"
    )
    # Migration 0007: audit_logs (two-level audit trail, tenant-capability-
    # gating-plan.md's follow-up).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_logs ("
        "id BIGSERIAL PRIMARY KEY, "
        "actor_level TEXT NOT NULL CHECK (actor_level IN ('platform_admin', 'portal')), "
        "hospital_id INTEGER REFERENCES hospitals(id), "
        "actor_label TEXT NOT NULL, "
        "action TEXT NOT NULL, "
        "entity_type TEXT, "
        "entity_id TEXT, "
        "before_value TEXT, "
        "after_value TEXT, "
        "created_at TEXT NOT NULL DEFAULT (now()::text)"
        ")"
    )
    # Self-healing: an environment that ran an interim draft of this
    # migration (created_at as a native timestamp type, before it was
    # corrected to TEXT to match this schema's own "every timestamp is
    # ISO-8601 TEXT" convention) already has the table, so the CREATE TABLE
    # IF NOT EXISTS above is a no-op there and never picks up the fix --
    # this ALTER converges it on every startup regardless of which shape it
    # currently has (a column already TEXT casts to itself, a no-op).
    conn.execute("ALTER TABLE audit_logs ALTER COLUMN created_at TYPE TEXT USING created_at::text")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_logs_hospital_created ON audit_logs (hospital_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_logs_level_created ON audit_logs (actor_level, created_at)")
    # Migration 0010: code_sequences (db/display_ids.py's shared, yearly-
    # resetting counter table for DCCG/DCCH/DCCC/DCCP -- see that module's
    # own docstring). Must run before seed_default_hospital()/
    # _backfill_patient_link_accounts() below, both of which now mint a
    # display_id through this table.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS code_sequences ("
        "id SERIAL PRIMARY KEY, "
        "prefix TEXT NOT NULL, "
        "scope_key TEXT NOT NULL, "
        "period_key TEXT NOT NULL, "
        "last_value INTEGER NOT NULL DEFAULT 0, "
        "UNIQUE (prefix, scope_key, period_key)"
        ")"
    )
    # Migration 0011: platform_settings (db/repositories/platform_settings.py)
    # -- a SINGLETON row (id=1, CHECK-enforced) for cross-tenant values only a
    # platform/super admin can change, starting with max_active_patient_links.
    # Seeded from db/models.py's DEFAULT_MAX_ACTIVE_PATIENT_LINKS exactly
    # once; ON CONFLICT DO NOTHING makes re-running this on every startup a
    # no-op once the row exists, so a later admin edit (admin/
    # platform_settings_api.py) is never silently reset back to the default.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS platform_settings ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "max_active_patient_links INTEGER NOT NULL DEFAULT 5"
        ")"
    )
    conn.execute(
        "INSERT INTO platform_settings (id, max_active_patient_links) VALUES (1, ?) "
        "ON CONFLICT (id) DO NOTHING",
        (DEFAULT_MAX_ACTIVE_PATIENT_LINKS,)
    )
    # Migration 0014: feature_labels/dpdp_consent_required move from a
    # per-hospital column to this ONE global row (db/repositories/
    # platform_settings.py's own docstring) -- additive, defaults keep every
    # existing deployment's menu labels blank / DPDP gate off until a
    # platform admin sets them via /api/admin/platform-settings.
    conn.execute("ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS feature_labels TEXT")
    conn.execute("ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS dpdp_consent_required BOOLEAN NOT NULL DEFAULT FALSE")
    conn.execute("UPDATE platform_settings SET feature_labels = '{}' WHERE feature_labels IS NULL")
    # Migration 0010: doctors.email/password_hash -- dedicated doctor login,
    # additive/nullable, admin-issued via portal/routes/doctors.py.
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS email TEXT")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS password_hash TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_doctors_email ON doctors(email) WHERE email IS NOT NULL"
    )
    # Migration 0013: staff_users/role_permissions/super_admins -- RBAC +
    # Redis (docs/rbac-redis-plan.md). New tables only, doctors.email/
    # password_hash and hospitals.portal_password_hash stay untouched here
    # (dropped only in a later cleanup migration, once every doctor/hospital
    # has a staff_users row) -- see db/schema.sql's own comment on these
    # three tables for the full reasoning.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS staff_users ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "role TEXT NOT NULL CHECK (role IN ('admin', 'receptionist', 'doctor')), "
        "email TEXT NOT NULL, "
        "password_hash TEXT NOT NULL, "
        "name TEXT NOT NULL, "
        "doctor_id TEXT REFERENCES doctors(id), "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
        "token_version INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (now()::text), "
        "updated_at TEXT NOT NULL DEFAULT (now()::text)"
        ")"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_staff_users_email ON staff_users(lower(email))")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_staff_users_doctor_id ON staff_users(doctor_id) "
        "WHERE doctor_id IS NOT NULL"
    )
    conn.execute("ALTER TABLE staff_users DROP CONSTRAINT IF EXISTS ck_staff_users_doctor_role_pairing")
    conn.execute(
        "ALTER TABLE staff_users ADD CONSTRAINT ck_staff_users_doctor_role_pairing "
        "CHECK ((role = 'doctor') = (doctor_id IS NOT NULL))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS role_permissions ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "role TEXT NOT NULL CHECK (role IN ('admin', 'receptionist', 'doctor')), "
        "page_key TEXT NOT NULL, "
        "can_view BOOLEAN NOT NULL DEFAULT FALSE, "
        "can_write BOOLEAN NOT NULL DEFAULT FALSE, "
        "can_delete BOOLEAN NOT NULL DEFAULT FALSE, "
        "UNIQUE(hospital_id, role, page_key)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_role_permissions_hospital_role ON role_permissions(hospital_id, role)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS super_admins ("
        "id SERIAL PRIMARY KEY, "
        "email TEXT NOT NULL UNIQUE, "
        "password_hash TEXT NOT NULL, "
        "name TEXT NOT NULL, "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
        "token_version INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (now()::text)"
        ")"
    )
    # Migration 0016: identities -- one shared identity/credential table for
    # every principal (see db/migrations/versions/0016_identities_merge.py's
    # own docstring). users/staff_users/super_admins/hospital_users above are
    # untouched, never dropped -- this block only adds the new tables and
    # backfills them; db/repositories/{users,staff_users,super_admins}.py now
    # read/write these instead.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS identities ("
        "id SERIAL PRIMARY KEY, "
        "email TEXT NOT NULL, "
        "name TEXT, "
        "google_id TEXT, "
        "password_hash TEXT, "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
        "token_version INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (now()::text), "
        "updated_at TEXT"
        ")"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_identities_email ON identities(lower(email))")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_identities_google_id ON identities(google_id) "
        "WHERE google_id IS NOT NULL"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS staff_details ("
        "identity_id INTEGER PRIMARY KEY REFERENCES identities(id), "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "role TEXT NOT NULL CHECK (role IN ('admin', 'receptionist', 'doctor')), "
        "doctor_id TEXT REFERENCES doctors(id)"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_staff_details_doctor_id ON staff_details(doctor_id) "
        "WHERE doctor_id IS NOT NULL"
    )
    conn.execute("ALTER TABLE staff_details DROP CONSTRAINT IF EXISTS ck_staff_details_doctor_role_pairing")
    conn.execute(
        "ALTER TABLE staff_details ADD CONSTRAINT ck_staff_details_doctor_role_pairing "
        "CHECK ((role = 'doctor') = (doctor_id IS NOT NULL))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS super_admin_details ("
        "identity_id INTEGER PRIMARY KEY REFERENCES identities(id)"
        ")"
    )
    # Transitional only (migration 0018 drops this further down): still
    # created here, on every boot, purely so the backfill below has
    # somewhere to land hospital_users/users data (or, on an existing
    # database, so any hospital_owners rows from before 0018 are read
    # rather than erroring) before everything is merged into staff_details
    # and this table is dropped again in the same call.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hospital_owners ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "identity_id INTEGER NOT NULL REFERENCES identities(id), "
        "role TEXT NOT NULL DEFAULT 'owner', "
        "created_at TEXT NOT NULL DEFAULT (now()::text), "
        "UNIQUE(hospital_id, identity_id)"
        ")"
    )
    conn.commit()
    # Backfill -- idempotent (every INSERT is ON CONFLICT DO NOTHING/DO
    # UPDATE), safe to re-run on every startup. Order matters: users (OAuth)
    # first via DO NOTHING so it never clobbers a password identity; see
    # 0016_identities_merge.py's docstring for the full merge-by-email
    # reasoning this mirrors exactly.
    conn.execute(
        "INSERT INTO identities (email, name, google_id, created_at) "
        "SELECT email, name, google_id, created_at FROM users "
        "ON CONFLICT (lower(email)) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO identities (email, name, password_hash, is_active, token_version, created_at) "
        "SELECT email, name, password_hash, is_active, token_version, created_at FROM staff_users "
        "ON CONFLICT (lower(email)) DO UPDATE SET "
        "password_hash = EXCLUDED.password_hash, is_active = EXCLUDED.is_active, "
        "token_version = EXCLUDED.token_version"
    )
    conn.execute(
        "INSERT INTO staff_details (identity_id, hospital_id, role, doctor_id) "
        "SELECT i.id, s.hospital_id, s.role, s.doctor_id "
        "FROM staff_users s JOIN identities i ON lower(i.email) = lower(s.email) "
        "ON CONFLICT (identity_id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO identities (email, name, password_hash, is_active, token_version, created_at) "
        "SELECT email, name, password_hash, is_active, token_version, created_at FROM super_admins "
        "ON CONFLICT (lower(email)) DO UPDATE SET "
        "password_hash = EXCLUDED.password_hash, is_active = EXCLUDED.is_active, "
        "token_version = EXCLUDED.token_version"
    )
    conn.execute(
        "INSERT INTO super_admin_details (identity_id) "
        "SELECT i.id FROM super_admins sa JOIN identities i ON lower(i.email) = lower(sa.email) "
        "ON CONFLICT (identity_id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO hospital_owners (hospital_id, identity_id, role, created_at) "
        "SELECT hu.hospital_id, i.id, hu.role, hu.created_at "
        "FROM hospital_users hu "
        "JOIN users u ON u.id = hu.user_id "
        "JOIN identities i ON lower(i.email) = lower(u.email) "
        "ON CONFLICT (hospital_id, identity_id) DO NOTHING"
    )
    # Migration 0018: hospital_owners folded into staff_details (role='admin')
    # -- confirmed with the user, the M:M ownership shape was never actually
    # used (no identity owns more than one hospital in practice), and a
    # hospital's role vocabulary stays exactly admin/receptionist/doctor, no
    # separate 'owner' role. Runs after the hospital_owners backfill above
    # (so any live data already in hospital_owners on an existing database
    # is captured) and before that table is dropped below.
    conn.execute(
        "INSERT INTO staff_details (identity_id, hospital_id, role, doctor_id) "
        "SELECT identity_id, hospital_id, 'admin', NULL FROM hospital_owners "
        "ON CONFLICT (identity_id) DO NOTHING"
    )
    conn.commit()
    # Migration 0017: the backfill above is done with these tables --
    # confirmed with the user, drop them now that every row lives in
    # identities/staff_details/super_admin_details. hospital_users first (its
    # user_id FK references users). On a database that already dropped these
    # (every startup after the first), each is a no-op via IF EXISTS.
    # _baseline_schema._BASELINE_SQL above still recreates users/hospital_users
    # on every startup (it's a frozen, unedited historical migration -- see
    # its own docstring) -- these DROPs are what actually keeps them gone.
    conn.execute("DROP TABLE IF EXISTS hospital_users")
    conn.execute("DROP TABLE IF EXISTS users")
    conn.execute("DROP TABLE IF EXISTS staff_users")
    conn.execute("DROP TABLE IF EXISTS super_admins")
    # Migration 0018: hospital_owners itself, now that its rows live in
    # staff_details (see the backfill just above).
    conn.execute("DROP TABLE IF EXISTS hospital_owners")
    # Migration 0015: appointment_types.is_allowed -- platform-admin whitelist
    # (edit-tenant page), independent of the tenant-controlled is_active
    # toggle. Defaults TRUE so no already-onboarded tenant loses access to
    # anything it could already use.
    conn.execute("ALTER TABLE appointment_types ADD COLUMN IF NOT EXISTS is_allowed BOOLEAN NOT NULL DEFAULT TRUE")
    # Migration 0019: care_connect_accounts.language -- a chosen language is
    # now persisted GLOBALLY on the account (same at every hospital this
    # person messages), not re-asked every session timeout. NULL (every
    # existing row) means "never chosen yet", same as today's behavior.
    conn.execute("ALTER TABLE care_connect_accounts ADD COLUMN IF NOT EXISTS language TEXT")
    # Migration 0019: handoff auto-resolve -- hospitals.handoff_auto_resolve_hours
    # (nullable, per-hospital threshold) + handoff_requests.resolved_by
    # ('auto' vs. a staff member's hashed session token).
    conn.execute("ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS handoff_auto_resolve_hours INTEGER")
    conn.execute("ALTER TABLE handoff_requests ADD COLUMN IF NOT EXISTS resolved_by TEXT")
    # Migration 0021: hospital_settings -- per-hospital self-serve settings
    # table (not more columns on `hospitals`), starting with Follow-up's
    # eligibility window + fee lines. See that migration's own docstring.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hospital_settings ("
        "hospital_id INTEGER PRIMARY KEY REFERENCES hospitals(id), "
        "followup_validity_days INTEGER, "
        "followup_fee NUMERIC(10, 2), "
        "new_consultation_fee NUMERIC(10, 2)"
        ")"
    )
    conn.execute("ALTER TABLE hospital_settings DROP CONSTRAINT IF EXISTS hospital_settings_followup_validity_days_check")
    conn.execute(
        "ALTER TABLE hospital_settings ADD CONSTRAINT hospital_settings_followup_validity_days_check "
        "CHECK (followup_validity_days IS NULL OR followup_validity_days > 0)"
    )
    conn.execute("ALTER TABLE hospital_settings DROP CONSTRAINT IF EXISTS hospital_settings_followup_fee_check")
    conn.execute(
        "ALTER TABLE hospital_settings ADD CONSTRAINT hospital_settings_followup_fee_check "
        "CHECK (followup_fee IS NULL OR followup_fee >= 0)"
    )
    conn.execute("ALTER TABLE hospital_settings DROP CONSTRAINT IF EXISTS hospital_settings_new_consultation_fee_check")
    conn.execute(
        "ALTER TABLE hospital_settings ADD CONSTRAINT hospital_settings_new_consultation_fee_check "
        "CHECK (new_consultation_fee IS NULL OR new_consultation_fee >= 0)"
    )
    # Migration 0022: patient_documents.document_type (WhatsApp menu
    # restructuring's Reports & Prescriptions category filter).
    conn.execute("ALTER TABLE patient_documents ADD COLUMN IF NOT EXISTS document_type TEXT")
    conn.execute("UPDATE patient_documents SET document_type = 'other' WHERE document_type IS NULL")
    conn.execute("ALTER TABLE patient_documents ALTER COLUMN document_type SET NOT NULL")
    conn.execute("ALTER TABLE patient_documents ALTER COLUMN document_type SET DEFAULT 'other'")
    # Migration 0023: relabel default appointment types (v2) -- Diagnostic
    # Test/Lab Test/Daycare / Procedure, matching the new Tests & Diagnostics
    # category submenu's row titles. Only rows still holding the OLD default
    # label are relabeled, same rule 0009's own relabel already applies.
    for _type_id, _old_label, _new_label in (
        ("diagnostic", "Diagnostic Booking", "Diagnostic Test"),
        ("lab", "Lab Test Booking", "Lab Test"),
        ("daycare", "Daycare Booking", "Daycare / Procedure"),
    ):
        conn.execute(
            "UPDATE appointment_types SET label = ? WHERE id = ? AND label = ?",
            (_new_label, _type_id, _old_label),
        )
    # Migration 0024: appointments.followup_override_until -- admin/
    # receptionist follow-up validity override (see db/schema.sql's own
    # column comment).
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS followup_override_until TEXT")
    # Migration 0025: diagnostic_tests/diagnostic_test_variants/
    # diagnostic_resources (+ leave/slots) and the appointments columns that
    # bind a booking to a resource/test/variant -- see db/schema.sql's own
    # comments on each table for the full reasoning (docs/per-appointment-
    # type-flow-plan.md Step 5).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS diagnostic_resources ("
        "id TEXT PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "department_id TEXT REFERENCES departments(id), name TEXT NOT NULL, "
        "working_days TEXT NOT NULL DEFAULT '', working_hours TEXT NOT NULL DEFAULT '', "
        "slot_duration_minutes INTEGER NOT NULL DEFAULT 30, breaks TEXT NOT NULL DEFAULT '', "
        "max_bookings_per_slot INTEGER NOT NULL DEFAULT 1, daily_booking_limit INTEGER, "
        "effective_from TEXT, is_active BOOLEAN NOT NULL DEFAULT TRUE"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS diagnostic_resource_leave ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "resource_id TEXT NOT NULL REFERENCES diagnostic_resources(id), date TEXT NOT NULL, reason TEXT, "
        "UNIQUE(resource_id, date)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS diagnostic_resource_slots ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "resource_id TEXT NOT NULL REFERENCES diagnostic_resources(id), scheduled_at TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (now()::text), "
        "blocked BOOLEAN NOT NULL DEFAULT FALSE, block_reason TEXT, "
        "UNIQUE(resource_id, scheduled_at)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS diagnostic_tests ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "category TEXT NOT NULL CHECK (category IN ('diagnostic', 'lab')), name TEXT NOT NULL, "
        "resource_id TEXT REFERENCES diagnostic_resources(id), "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE, sort_order INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS diagnostic_test_variants ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "test_id INTEGER NOT NULL REFERENCES diagnostic_tests(id), label TEXT NOT NULL, "
        "price NUMERIC(10, 2), preparation_instructions TEXT, "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE, sort_order INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute("ALTER TABLE appointments ALTER COLUMN doctor_id DROP NOT NULL")
    # diagnostic_test_id itself (the column that used to be two separate,
    # duplicate columns -- resource_id and diagnostic_test_id, merged into
    # one) is added further down, once diagnostic_tests exists in its final
    # form and diagnostic_resources is gone -- see that section's own
    # comment. Nothing here needs it early.
    conn.execute(
        "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostic_test_variant_id "
        "INTEGER REFERENCES diagnostic_test_variants(id)"
    )
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostic_test_label TEXT")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostic_variant_label TEXT")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostic_price NUMERIC(10, 2)")
    # Production incident: this block used to DROP+re-ADD the NARROW
    # "doctor_id IS NOT NULL OR resource_id IS NOT NULL" constraint here on
    # every single boot (init_db_on_connection() replays every delta
    # unconditionally, not just once) -- fine on a fresh/empty table, but
    # once real procedure-only appointments exist in production (doctor_id
    # AND resource_id both NULL, only procedure_id set -- migration 0028's
    # own case), re-narrowing the constraint even momentarily fails against
    # that already-existing, perfectly valid data, crashing the app on
    # every restart. The WIDE constraint (including procedure_id) further
    # down this same function is the only one that should ever actually be
    # created here -- dropping the narrow one (if it still exists from an
    # older deploy) is enough; it must never be recreated in this idempotent
    # path.
    conn.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_doctor_or_resource_chk")
    # The unique index that used to be recreated here (ux_appointments_
    # resource_slot_ordinal_booked, now ux_appointments_diagnostic_test_
    # slot_ordinal_booked) needs diagnostic_test_id to already exist --
    # moved to just after that column's own creation, further down.
    # Migration 0026: Lab Test's multi-test basket (appointment_lab_tests),
    # collection-method/home-collection columns on appointments, the
    # hospital-configurable serviceable-PIN-code list (lab_service_areas),
    # and the flat home_collection_charge fee on hospital_settings -- see
    # that migration's own docstring.
    conn.execute("ALTER TABLE hospital_settings ADD COLUMN IF NOT EXISTS home_collection_charge NUMERIC(10, 2)")
    conn.execute("ALTER TABLE hospital_settings DROP CONSTRAINT IF EXISTS hospital_settings_home_collection_charge_check")
    conn.execute(
        "ALTER TABLE hospital_settings ADD CONSTRAINT hospital_settings_home_collection_charge_check "
        "CHECK (home_collection_charge IS NULL OR home_collection_charge >= 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lab_service_areas ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "pincode TEXT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT TRUE, "
        "UNIQUE(hospital_id, pincode)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS appointment_lab_tests ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "appointment_id INTEGER NOT NULL REFERENCES appointments(id), "
        "diagnostic_test_id INTEGER REFERENCES diagnostic_tests(id), "
        "diagnostic_test_variant_id INTEGER REFERENCES diagnostic_test_variants(id), "
        "test_label TEXT NOT NULL, variant_label TEXT NOT NULL, "
        "price NUMERIC(10, 2), preparation_instructions TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_appointment_lab_tests_appointment "
        "ON appointment_lab_tests(appointment_id)"
    )
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS collection_method TEXT")
    conn.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_collection_method_check")
    conn.execute(
        "ALTER TABLE appointments ADD CONSTRAINT appointments_collection_method_check "
        "CHECK (collection_method IS NULL OR collection_method IN ('visit', 'home'))"
    )
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS collection_address TEXT")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS collection_pincode TEXT")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS home_collection_charge NUMERIC(10, 2)")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS lab_status TEXT")
    conn.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_lab_status_check")
    conn.execute(
        "ALTER TABLE appointments ADD CONSTRAINT appointments_lab_status_check "
        "CHECK (lab_status IS NULL OR lab_status IN ('booked', 'sample_collected', 'processing', 'report_ready'))"
    )
    # Migration 0027: google_calendar_connections -- Google Meet integration,
    # alongside (not replacing) the existing Jitsi tele-consultation link.
    # Renumbered 0022 -> 0025 -> 0027 across two separate migration-number
    # collisions with concurrent branches forked from the same parent. See
    # that migration's own docstring.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS google_calendar_connections ("
        "doctor_id TEXT PRIMARY KEY REFERENCES doctors(id), "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "google_email TEXT, "
        "encrypted_access_token TEXT NOT NULL, "
        "encrypted_refresh_token TEXT NOT NULL, "
        "access_token_expires_at TEXT NOT NULL, "
        "calendar_id TEXT NOT NULL DEFAULT 'primary', "
        "connected_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL"
        ")"
    )
    # Migration 0029: one Google account per HOSPITAL (an admin connects it
    # once, used for every doctor's tele-consultation Meet links), not one
    # per doctor -- see that migration's own docstring for why this is a
    # data-preserving repoint of the primary key rather than a destructive
    # rebuild. Idempotent/one-time via the same "does the old column still
    # exist" guard db/migrations/env.py's own idempotent deltas use
    # elsewhere: a fresh boot after this ships transitions the table once;
    # every boot after that, `doctor_id` is already gone and this whole
    # block is a no-op.
    conn.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'google_calendar_connections' AND column_name = 'doctor_id') THEN "
        "DELETE FROM google_calendar_connections "
        "WHERE doctor_id NOT IN ("
        "    SELECT DISTINCT ON (hospital_id) doctor_id FROM google_calendar_connections "
        "    ORDER BY hospital_id, updated_at DESC"
        "); "
        "ALTER TABLE google_calendar_connections DROP CONSTRAINT google_calendar_connections_pkey; "
        "ALTER TABLE google_calendar_connections DROP COLUMN doctor_id; "
        "ALTER TABLE google_calendar_connections ADD PRIMARY KEY (hospital_id); "
        "END IF; "
        "END $$;"
    )
    # Migration 0028: Daycare/Procedure rebuild -- replaces the old duration-
    # picker model entirely (daycare_duration_options/appointments.duration_hours,
    # migration 0008, dropped below) with a procedure catalog, resource pools
    # (bed/chair + equipment + staff), and an approval workflow. See that
    # migration's own docstring.
    conn.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_duration_hours_check")
    conn.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS duration_hours")
    conn.execute("DROP TABLE IF EXISTS daycare_duration_options")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS procedures ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "category TEXT NOT NULL CHECK (category IN ("
        "'chemotherapy', 'dialysis', 'infusion_therapy', 'dressing_wound_care', "
        "'injection', 'minor_procedure', 'other')), "
        "name TEXT NOT NULL, "
        "department_id TEXT REFERENCES departments(id), "
        "booking_mode TEXT NOT NULL CHECK (booking_mode IN ('instant', 'approval_required')), "
        "duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0), "
        "estimated_price_min NUMERIC(10, 2), "
        "estimated_price_max NUMERIC(10, 2), "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
        "sort_order INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS procedure_required_resource_types ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "procedure_id INTEGER NOT NULL REFERENCES procedures(id), "
        "resource_type TEXT NOT NULL CHECK (resource_type IN ('bed_chair', 'equipment', 'staff')), "
        "UNIQUE(procedure_id, resource_type)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS procedure_instructions ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "procedure_id INTEGER NOT NULL REFERENCES procedures(id), "
        "instruction_type TEXT NOT NULL CHECK (instruction_type IN ("
        "'documents', 'preparation', 'arrival_time', 'medication', 'insurance_authorization', 'other')), "
        "instruction_text TEXT NOT NULL, "
        "sort_order INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS procedure_resources ("
        "id TEXT PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "resource_type TEXT NOT NULL CHECK (resource_type IN ('bed_chair', 'equipment', 'staff')), "
        "department_id TEXT REFERENCES departments(id), "
        "name TEXT NOT NULL, "
        "working_days TEXT NOT NULL DEFAULT '', "
        "working_hours TEXT NOT NULL DEFAULT '', "
        "slot_duration_minutes INTEGER NOT NULL DEFAULT 30, "
        "breaks TEXT NOT NULL DEFAULT '', "
        "max_bookings_per_slot INTEGER NOT NULL DEFAULT 1, "
        "daily_booking_limit INTEGER, "
        "effective_from TEXT, is_active BOOLEAN NOT NULL DEFAULT TRUE"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS procedure_resource_leave ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "resource_id TEXT NOT NULL REFERENCES procedure_resources(id), date TEXT NOT NULL, reason TEXT, "
        "UNIQUE(resource_id, date)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS procedure_resource_slots ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "resource_id TEXT NOT NULL REFERENCES procedure_resources(id), scheduled_at TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (now()::text), "
        "blocked BOOLEAN NOT NULL DEFAULT FALSE, block_reason TEXT, "
        "UNIQUE(resource_id, scheduled_at)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS appointment_procedure_resources ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "appointment_id INTEGER NOT NULL REFERENCES appointments(id), "
        "resource_id TEXT NOT NULL REFERENCES procedure_resources(id), "
        "resource_type TEXT NOT NULL CHECK (resource_type IN ('bed_chair', 'equipment', 'staff')), "
        "resource_name TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_appointment_procedure_resources_appointment "
        "ON appointment_procedure_resources(appointment_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_appointment_procedure_resources_resource "
        "ON appointment_procedure_resources(resource_id)"
    )
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS procedure_id INTEGER REFERENCES procedures(id)")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS procedure_status TEXT")
    conn.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_procedure_status_check")
    conn.execute(
        # NOT VALID: this drop+recreate runs on EVERY startup (not just once),
        # and a plain ADD CONSTRAINT CHECK validates against every existing
        # row at add-time -- one stray bad row would otherwise crash every
        # future boot until someone finds and fixes it by hand. NOT VALID
        # still enforces the check for every new insert/update from this
        # point on; it only skips re-validating pre-existing rows.
        "ALTER TABLE appointments ADD CONSTRAINT appointments_procedure_status_check "
        "CHECK (procedure_status IS NULL OR procedure_status IN "
        "('REQUESTED', 'UNDER_REVIEW', 'APPROVED', 'REJECTED', 'CONFIRMED', 'COMPLETED', 'CANCELLED')) NOT VALID"
    )
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS procedure_estimated_price_min NUMERIC(10, 2)")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS procedure_estimated_price_max NUMERIC(10, 2)")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS procedure_order_reference TEXT")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS procedure_reschedule_requested_at TEXT")
    conn.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_doctor_or_resource_chk")
    conn.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_doctor_or_resource_or_procedure_chk")
    # The re-ADD (NOT VALID, same rationale as appointments_procedure_status_
    # check above) needs diagnostic_test_id to already exist -- moved to just
    # after that column's own creation, further down, right next to the
    # unique index that has the same dependency.
    # Migration 0030: lab_service_areas gains range_start/range_end so a
    # hospital can add a PIN-code range, not just individual codes.
    conn.execute("ALTER TABLE lab_service_areas ALTER COLUMN pincode DROP NOT NULL")
    conn.execute("ALTER TABLE lab_service_areas ADD COLUMN IF NOT EXISTS range_start TEXT")
    conn.execute("ALTER TABLE lab_service_areas ADD COLUMN IF NOT EXISTS range_end TEXT")
    conn.execute("ALTER TABLE lab_service_areas DROP CONSTRAINT IF EXISTS lab_service_areas_single_xor_range_chk")
    conn.execute(
        # NOT VALID -- see appointments_procedure_status_check's own comment
        # above for why.
        "ALTER TABLE lab_service_areas ADD CONSTRAINT lab_service_areas_single_xor_range_chk "
        "CHECK ((pincode IS NOT NULL AND range_start IS NULL AND range_end IS NULL) OR "
        "(pincode IS NULL AND range_start IS NOT NULL AND range_end IS NOT NULL AND range_start <= range_end)) NOT VALID"
    )
    # Migration 0031: hospital-configurable slot-generation window, replacing
    # the hardcoded 14-day _SLOT_DAYS_AHEAD default.
    conn.execute("ALTER TABLE hospital_settings ADD COLUMN IF NOT EXISTS future_booking_days INTEGER")
    conn.execute("ALTER TABLE hospital_settings DROP CONSTRAINT IF EXISTS hospital_settings_future_booking_days_check")
    conn.execute(
        "ALTER TABLE hospital_settings ADD CONSTRAINT hospital_settings_future_booking_days_check "
        "CHECK (future_booking_days IS NULL OR future_booking_days > 0)"
    )
    # Migration 0032: doctor_slot_overrides replaces bulk-pre-generated
    # doctor_slots -- a doctor's normal grid is computed live now, this table
    # only ever holds a row for a slot staff has actually blocked or
    # custom-added. No data migration needed here (unlike the real Alembic
    # migration) -- init_db_on_connection() only ever runs against a fresh
    # test schema with no pre-existing doctor_slots data to carry over.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS doctor_slot_overrides ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "doctor_id TEXT NOT NULL REFERENCES doctors(id), scheduled_at TEXT NOT NULL, "
        "is_custom BOOLEAN NOT NULL DEFAULT FALSE, blocked BOOLEAN NOT NULL DEFAULT FALSE, "
        "block_reason TEXT, "
        "UNIQUE(doctor_id, scheduled_at)"
        ")"
    )
    conn.execute("DROP TABLE IF EXISTS doctor_slots")
    # Migration 0033: a queued future schedule change (see that migration's
    # own docstring) -- read at compute time, not promoted by any job.
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS pending_working_days TEXT")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS pending_working_hours TEXT")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS pending_slot_duration_minutes INTEGER")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS pending_breaks TEXT")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS pending_daily_booking_limit INTEGER")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS pending_effective_from TEXT")
    # Migration 0034: outright-removed slots are their own exception,
    # distinct from `blocked` -- see that migration's own docstring.
    conn.execute(
        "ALTER TABLE doctor_slot_overrides ADD COLUMN IF NOT EXISTS excluded BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # Migration 0035: a resource-bound booking can have no department at all
    # -- see that migration's own docstring.
    conn.execute("ALTER TABLE appointments ALTER COLUMN department_id DROP NOT NULL")
    # Migration 0036: diagnostic tests/resources merge -- a diagnostic_tests
    # row now carries its own schedule directly (no separate
    # diagnostic_resources row, no department) -- see that migration's own
    # docstring. No data was carried forward from diagnostic_resources at
    # the time of this ONE-TIME cutover (old resource ids pointed at a table
    # being retired, so there was nothing sane to map them to).
    #
    # 2026-09-11 incident: the DROP INDEX/DROP COLUMN pair that used to sit
    # here (paired with the ADD COLUMN IF NOT EXISTS/CREATE UNIQUE INDEX IF
    # NOT EXISTS below) was written in the same "safe to re-run forever"
    # style as every other statement in this file, but a DROP COLUMN is NOT
    # idempotent the way ADD COLUMN IF NOT EXISTS/DROP ... IF EXISTS on an
    # already-gone object are -- every boot, it deleted appointments.resource_id
    # (and every value in it) and the ADD COLUMN right below immediately
    # recreated it empty, so it always found the column present to destroy
    # again on the NEXT boot too. In effect, appointments.resource_id was
    # silently wiped to NULL on every single app restart, for every
    # resource-bound (Diagnostics/Lab) appointment, from the moment this
    # migration first ran going forward -- not a one-time cutover at all.
    # Removed entirely now that the actual one-time cutover has already
    # happened in every environment that reaches this code -- there is
    # nothing left to migrate, so this block is just the ordinary permanent
    # column/index definition below, exactly like every other table in this
    # file.
    #
    # 2026-09-11, later same day: resource_id itself was then merged into
    # what used to be a second, fully-redundant appointments.diagnostic_test_id
    # column (confirmed with the user: every write path set both to the same
    # value, or left diagnostic_test_id NULL -- see the real migration,
    # 20260911135450_merge_appointments_resource_id_into_.py, for the
    # production ALTER TABLE ... RENAME COLUMN). This raw-SQL baseline never
    # renames anything -- tests bypass Alembic and rebuild from an empty
    # schema every time, so it just needs to arrive at the same end state
    # directly: no early resource_id/diagnostic_test_id placeholder column
    # (the old two-phase "placeholder now, real column later" shape below
    # depended on one existing early only because DROP TABLE diagnostic_
    # resources at the end of this block needs every FK into it gone first;
    # removing the placeholder entirely removes that FK too, so nothing
    # needs cleaning up here anymore), and diagnostic_test_id created ONCE,
    # correctly typed, right below.
    conn.execute("ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS working_days TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS working_hours TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS slot_duration_minutes INTEGER NOT NULL DEFAULT 30"
    )
    conn.execute("ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS breaks TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS max_bookings_per_slot INTEGER NOT NULL DEFAULT 1"
    )
    conn.execute("ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS daily_booking_limit INTEGER")
    conn.execute("ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS effective_from TEXT")
    conn.execute("ALTER TABLE diagnostic_tests DROP COLUMN IF EXISTS resource_id")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS diagnostic_test_slots ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "test_id INTEGER NOT NULL REFERENCES diagnostic_tests(id), scheduled_at TEXT NOT NULL, "
        "blocked BOOLEAN NOT NULL DEFAULT FALSE, block_reason TEXT, "
        "UNIQUE(test_id, scheduled_at)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS diagnostic_test_leave ("
        "id SERIAL PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "test_id INTEGER NOT NULL REFERENCES diagnostic_tests(id), date TEXT NOT NULL, reason TEXT, "
        "UNIQUE(test_id, date)"
        ")"
    )
    conn.execute(
        "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS diagnostic_test_id INTEGER REFERENCES diagnostic_tests(id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_appointments_diagnostic_test_slot_ordinal_booked ON appointments "
        "(diagnostic_test_id, scheduled_at, booking_ordinal) WHERE status = 'booked' AND diagnostic_test_id IS NOT NULL"
    )
    conn.execute(
        # NOT VALID -- see appointments_procedure_status_check's own comment
        # above for why (this is the constraint that has actually bitten us:
        # 2026-09-11, three separate incidents, each one a stray row with
        # doctor_id/diagnostic_test_id/procedure_id all NULL blocking every
        # future startup until found and deleted by hand). Re-ADDed here
        # (rather than back where it's DROPped, further up) because it needs
        # diagnostic_test_id to already exist -- same reason the unique
        # index above moved down here too.
        "ALTER TABLE appointments ADD CONSTRAINT appointments_doctor_or_resource_or_procedure_chk "
        "CHECK (doctor_id IS NOT NULL OR diagnostic_test_id IS NOT NULL OR procedure_id IS NOT NULL) NOT VALID"
    )
    conn.execute("DROP TABLE IF EXISTS diagnostic_resource_slots")
    conn.execute("DROP TABLE IF EXISTS diagnostic_resource_leave")
    conn.execute("DROP TABLE IF EXISTS diagnostic_resources")
    # Migration 0037: diagnostic tests/variants merge -- a diagnostic_tests
    # row now carries its own price directly (it only ever needed exactly
    # one priced option), so the separate diagnostic_test_variants child
    # table (and its unused preparation_instructions field) is gone -- see
    # that migration's own docstring. No data is carried forward.
    conn.execute("ALTER TABLE diagnostic_tests ADD COLUMN IF NOT EXISTS price NUMERIC(10, 2)")
    conn.execute("ALTER TABLE appointment_lab_tests DROP COLUMN IF EXISTS diagnostic_test_variant_id")
    conn.execute("ALTER TABLE appointment_lab_tests DROP COLUMN IF EXISTS variant_label")
    conn.execute("ALTER TABLE appointment_lab_tests DROP COLUMN IF EXISTS preparation_instructions")
    conn.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS diagnostic_test_variant_id")
    conn.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS diagnostic_variant_label")
    conn.execute("DROP TABLE IF EXISTS diagnostic_test_variants")
    # Migration 20260911162026: patient age -> date of birth (confirmed with
    # the user). age is dropped outright, no fallback -- an existing patient
    # with no date_of_birth on file just shows a blank age until one is
    # entered. appointments' denormalized snapshot moves from patient_age to
    # patient_date_of_birth, same role (the legacy duplicate-booking check
    # that tells apart two family members sharing one phone), just DOB-keyed.
    conn.execute("ALTER TABLE patients DROP COLUMN IF EXISTS age")
    conn.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS patient_age")
    conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS patient_date_of_birth TEXT")
    # Migration 20260911174439: staff_details gains department_id/phone/
    # address/shift/reports_to_id/attendance_status -- the Staff page's
    # mocked columns becoming real. department_id is doctor-role-exclusive
    # (a doctor row's department already comes from doctor_id ->
    # doctors.department_id) and attendance_status is a manually-set
    # current status with no history table behind it -- see that
    # migration's own docstring.
    conn.execute("ALTER TABLE staff_details ADD COLUMN IF NOT EXISTS department_id TEXT REFERENCES departments(id)")
    conn.execute("ALTER TABLE staff_details ADD COLUMN IF NOT EXISTS phone TEXT")
    conn.execute("ALTER TABLE staff_details ADD COLUMN IF NOT EXISTS address TEXT")
    conn.execute("ALTER TABLE staff_details ADD COLUMN IF NOT EXISTS shift TEXT")
    conn.execute("ALTER TABLE staff_details ADD COLUMN IF NOT EXISTS reports_to_id INTEGER REFERENCES identities(id)")
    conn.execute("ALTER TABLE staff_details ADD COLUMN IF NOT EXISTS attendance_status TEXT NOT NULL DEFAULT 'present'")
    conn.execute("ALTER TABLE staff_details DROP CONSTRAINT IF EXISTS ck_staff_details_shift")
    conn.execute(
        "ALTER TABLE staff_details ADD CONSTRAINT ck_staff_details_shift "
        "CHECK (shift IS NULL OR shift IN ('day', 'evening', 'night'))"
    )
    conn.execute("ALTER TABLE staff_details DROP CONSTRAINT IF EXISTS ck_staff_details_attendance_status")
    conn.execute(
        "ALTER TABLE staff_details ADD CONSTRAINT ck_staff_details_attendance_status "
        "CHECK (attendance_status IN ('present', 'on_leave', 'half_day'))"
    )
    conn.execute("ALTER TABLE staff_details DROP CONSTRAINT IF EXISTS ck_staff_details_department_doctor_role")
    conn.execute(
        "ALTER TABLE staff_details ADD CONSTRAINT ck_staff_details_department_doctor_role "
        "CHECK (role != 'doctor' OR department_id IS NULL)"
    )
    conn.execute("ALTER TABLE staff_details DROP CONSTRAINT IF EXISTS ck_staff_details_reports_to_not_self")
    conn.execute(
        "ALTER TABLE staff_details ADD CONSTRAINT ck_staff_details_reports_to_not_self "
        "CHECK (reports_to_id IS NULL OR reports_to_id != identity_id)"
    )
    # Migration 20260911190007: doctors gains phone/employee_id/location --
    # the Doctors page's mocked columns becoming real. phone/employee_id join
    # specialization/qualification as mandatory fields (confirmed with the
    # user); location stays optional. Existing rows are backfilled to ''
    # before the NOT NULL is added so this never fails against a hospital
    # that already has doctors on file, and all four get a DB-level
    # server_default of '' so a raw INSERT that never mentions them (db/
    # seed.py's default-hospital seeding, e.g.) keeps working unchanged --
    # the Doctors page's own Add/Edit form and CSV import are what actually
    # block a blank value (admin/validation.py's _validate_doctor_fields()).
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS phone TEXT")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS employee_id TEXT")
    conn.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS location TEXT")
    conn.execute("UPDATE doctors SET phone = '' WHERE phone IS NULL")
    conn.execute("UPDATE doctors SET employee_id = '' WHERE employee_id IS NULL")
    conn.execute("UPDATE doctors SET specialization = '' WHERE specialization IS NULL")
    conn.execute("UPDATE doctors SET qualification = '' WHERE qualification IS NULL")
    conn.execute("ALTER TABLE doctors ALTER COLUMN phone SET DEFAULT ''")
    conn.execute("ALTER TABLE doctors ALTER COLUMN employee_id SET DEFAULT ''")
    conn.execute("ALTER TABLE doctors ALTER COLUMN specialization SET DEFAULT ''")
    conn.execute("ALTER TABLE doctors ALTER COLUMN qualification SET DEFAULT ''")
    conn.execute("ALTER TABLE doctors ALTER COLUMN phone SET NOT NULL")
    conn.execute("ALTER TABLE doctors ALTER COLUMN employee_id SET NOT NULL")
    conn.execute("ALTER TABLE doctors ALTER COLUMN specialization SET NOT NULL")
    conn.execute("ALTER TABLE doctors ALTER COLUMN qualification SET NOT NULL")
    # Migration 20260911190251: drops the old dedicated doctor-login
    # credential columns (email/password_hash, migration 0012) -- dead code,
    # confirmed unused by any frontend UI; every doctor login now goes
    # through the unified staff login instead (staff_details role='doctor').
    conn.execute("DROP INDEX IF EXISTS ux_doctors_email")
    conn.execute("ALTER TABLE doctors DROP COLUMN IF EXISTS password_hash")
    conn.execute("ALTER TABLE doctors DROP COLUMN IF EXISTS email")
    # Migration 20260912065049: Leave Requests admin page -- hospitals gains
    # the portal-admin-configurable annual leave policy (doctor/receptionist
    # only, confirmed with the user), and leave_requests is the new
    # pending/approved/rejected review queue. See that migration's own
    # docstring for why identity_id, not staff_details, is the applicant FK.
    conn.execute("ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS doctor_annual_leave_days INTEGER NOT NULL DEFAULT 20")
    conn.execute("ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS staff_annual_leave_days INTEGER NOT NULL DEFAULT 30")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leave_requests ("
        "id SERIAL PRIMARY KEY, "
        "hospital_id INTEGER NOT NULL REFERENCES hospitals(id), "
        "identity_id INTEGER NOT NULL REFERENCES identities(id), "
        "leave_type TEXT NOT NULL, "
        "from_date TEXT NOT NULL, "
        "to_date TEXT NOT NULL, "
        "reason TEXT, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "decided_by INTEGER REFERENCES identities(id), "
        "decided_at TEXT, "
        "created_at TEXT NOT NULL DEFAULT now()::text"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_leave_requests_hospital_id ON leave_requests(hospital_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_leave_requests_identity_id ON leave_requests(identity_id)"
    )
    conn.execute("ALTER TABLE leave_requests DROP CONSTRAINT IF EXISTS ck_leave_requests_leave_type")
    conn.execute(
        "ALTER TABLE leave_requests ADD CONSTRAINT ck_leave_requests_leave_type "
        "CHECK (leave_type IN ('casual', 'sick', 'annual', 'maternity', 'conference', 'personal'))"
    )
    conn.execute("ALTER TABLE leave_requests DROP CONSTRAINT IF EXISTS ck_leave_requests_status")
    conn.execute(
        "ALTER TABLE leave_requests ADD CONSTRAINT ck_leave_requests_status "
        "CHECK (status IN ('pending', 'approved', 'rejected'))"
    )
    conn.execute("ALTER TABLE leave_requests DROP CONSTRAINT IF EXISTS ck_leave_requests_date_order")
    conn.execute(
        "ALTER TABLE leave_requests ADD CONSTRAINT ck_leave_requests_date_order CHECK (to_date >= from_date)"
    )
    # Migration 20260912141027: Settings -> Departments tab -- departments
    # gains a profile (floor_wing/consultation_hours/description/
    # head_doctor_id), an internal is_active status, and three patient-
    # facing visibility flags. show_on_frontend/whatsapp_booking_enabled
    # both gate the one real patient channel this app has (the WhatsApp
    # department picker, see db.get_departments()); online_booking_enabled
    # is stored/toggleable only -- no separate online booking channel
    # exists yet to enforce it in (confirmed with the user). All four
    # booleans default true so an existing department stays exactly as
    # visible/bookable as it always was.
    conn.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS floor_wing TEXT")
    conn.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS consultation_hours TEXT")
    conn.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS description TEXT")
    conn.execute(
        "ALTER TABLE departments ADD COLUMN IF NOT EXISTS head_doctor_id TEXT "
        "REFERENCES doctors(id) ON DELETE SET NULL"
    )
    conn.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
    conn.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS show_on_frontend BOOLEAN NOT NULL DEFAULT TRUE")
    conn.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS online_booking_enabled BOOLEAN NOT NULL DEFAULT TRUE")
    conn.execute("ALTER TABLE departments ADD COLUMN IF NOT EXISTS whatsapp_booking_enabled BOOLEAN NOT NULL DEFAULT TRUE")
    # Migration 5483e272c7c1: patients.duplicate_of_patient_id/
    # duplicate_flag_reason -- possible-duplicate review flag, stamped once
    # at creation by db/repositories/patients.py's _flag_duplicate_if_matches().
    conn.execute(
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS duplicate_of_patient_id INTEGER "
        "REFERENCES patients(id) ON DELETE SET NULL"
    )
    conn.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS duplicate_flag_reason TEXT")
    conn.commit()
    _settings = get_settings()
    hospital_name = _settings.HOSPITAL_NAME
    phone_number_id = _settings.WHATSAPP_PHONE_NUMBER_ID
    # Populating these from .env keeps the one real hospital's row usable for
    # per-message routing (SPEC Section 12.2/Phase 9) without requiring a manual
    # DB edit — core/main.py no longer reads WHATSAPP_ACCESS_TOKEN directly.
    access_token = _settings.WHATSAPP_ACCESS_TOKEN
    app_secret = _settings.WHATSAPP_APP_SECRET
    hospital_id = seed.seed_default_hospital(
        conn, hospital_name=hospital_name, whatsapp_phone_number_id=phone_number_id,
        access_token=access_token, app_secret=app_secret,
    )
    conn.commit()
    _backfill_enabled_features(conn)
    _backfill_patients(conn)
    _backfill_appointment_patient_denorm(conn)
    _backfill_appointment_patient_date_of_birth(conn)
    _backfill_patient_display_ids(conn)
    _backfill_patient_mrns(conn)
    _backfill_patient_links(conn)
    _backfill_appointment_types(conn)
    _backfill_procedures(conn)
    _backfill_diagnostic_tests(conn)
    _backfill_reports_prescriptions_feature(conn)
    _backfill_book_doctor_tests_diagnostics_split(conn)
    _backfill_admin_capabilities(conn)
    _backfill_diagnostic_resources_capability(conn)
    _backfill_procedures_capability(conn)
    _backfill_handoff_messages(conn)
    return hospital_id


def init_db() -> int:
    """
    Initializes whichever connection db.connection.get_connection() resolves to
    — the Postgres database at DATABASE_URL. Deliberately reuses that same
    shared connection (rather than opening + closing its own) so init_db() and
    every db/repository.py call afterward operate against the exact same
    connection object.
    Returns the seeded hospital's id.
    """
    run_alembic_migrations()
    conn = get_connection()
    return init_db_on_connection(conn)


def _redact_credentials(database_url: str) -> str:
    """Never print a password to stdout, even in a diagnostic CLI message --
    someone pasting this output into a bug report/Slack thread is the
    realistic leak vector, not an attacker with a debugger."""
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", database_url)


if __name__ == "__main__":
    seeded_hospital_id = init_db()
    print(f"Database initialized at {_redact_credentials(get_database_url())} (hospital_id={seeded_hospital_id})")