# db/repositories/patients.py
"""Patient search/directory, profiles, and the patient-identity-separation
linking/consent model (Spec.md Section 0). Split out of db/repository.py --
see ARCHITECTURE_PLAN.md Phase 1."""
from datetime import date, datetime
from typing import cast

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult

from db.connection import get_connection, get_session
from db.models import (
    STATUS_ATTENDED, STATUS_BOOKED, DuplicateSelfLinkError, TooManyLinkedPatientsError, _generate_patient_identifiers,
)
from db.orm_models import (
    AppointmentReminder, AppointmentRow, Department, DoctorRow, PatientDocument, PatientLink, PatientRow,
    PatientVisitNote,
)
from db.repositories.accounts import _get_or_create_account_in_conn

# --- Patients (Section 12.9 -- staff-created bookings need to search by name,
# not just phone; see db/schema.sql's comment on the patients table and
# create_appointment()'s _upsert_patient() for how rows get here) ---

def age_from_dob(date_of_birth: str | None) -> int | None:
    """Age is computed here, on read, from date_of_birth -- confirmed with
    the user: age itself is never collected or stored again (patients.age
    dropped outright, no fallback), so every response shape that used to
    return a stored age now returns this instead. None for no/malformed DOB."""
    if not date_of_birth:
        return None
    try:
        dob = date.fromisoformat(date_of_birth)
    except (TypeError, ValueError):
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def is_valid_phone(phone: str | None) -> bool:
    """Deliberately permissive (SPEC Section 12.9's phone-validation follow-up)
    -- rejects only the unambiguous garbage cases (empty, whitespace-only, or
    containing no digits at all, e.g. "not-a-phone-number!!"), not a strict
    phone-number format spec: no length requirement, no country-code check,
    no separator/whitespace-shape rules. International phone formats vary too
    much to validate meaningfully without a dedicated library this project
    doesn't otherwise need -- filtering garbage, not enforcing a spec, is the
    actual goal here. Called at every point a phone number is first captured:
    core/main.py's webhook intake (WhatsApp) and portal.py's new-booking form
    (staff) -- not re-checked here in create_appointment() itself, since both
    of those are the only real entry points and re-validating a third time
    at the shared data-access layer would just be the same rule maintained
    in three places instead of two."""
    if not phone:
        return False
    phone = phone.strip()
    if not phone:
        return False
    return any(c.isdigit() for c in phone)


def search_patients(hospital_id: int, query: str, limit: int = 10) -> list[dict]:
    """Case-insensitive partial match on name OR phone, hospital-scoped.
    Powers portal.py's /portal/patients/search (staff typing into the new-
    booking form's patient search box)."""
    query = query.strip()
    if not query:
        return []
    session = get_session()
    like = f"%{query}%"
    rows = session.execute(
        select(PatientRow.phone, PatientRow.name)
        .where(PatientRow.hospital_id == hospital_id, or_(PatientRow.phone.ilike(like), PatientRow.name.ilike(like)))
        .order_by(PatientRow.name.nulls_last(), PatientRow.phone)
        .limit(limit)
    ).all()
    return [{"phone": r.phone, "name": r.name} for r in rows]


def _patients_with_visit_stats_stmt(hospital_id: int, search: str | None = None):
    """Shared by list_patients()/get_recent_patients() -- last_visit is the
    most recent scheduled_at across every appointment (any status, same
    "staff want to see the full history" reasoning as
    get_all_appointments_for_hospital()), visit_count counts every
    appointment row ever created for that patient, not just kept ones
    (i.e. total ever booked, past or upcoming, any status). visited_count
    is the subset actually marked status='attended' -- a real, staff-
    confirmed outcome (Item 9, Spec.md Section 0), not a time-passed
    heuristic.

    Joined on patient_id, NOT phone -- a phone-keyed join here would
    silently mis-attribute visits once patients.phone can be the patient's
    OWN contact number ("Myself / Someone Else" registration,
    flows/patient_identity.py) rather than the WhatsApp number that
    actually booked the appointment (e.g. a child registered under a
    parent's WhatsApp: appointments.phone is the parent's number,
    patients.phone is now the child's own). patient_id is the authoritative
    link regardless of whose phone did the booking."""
    last_visit = func.max(AppointmentRow.scheduled_at)
    visit_count = func.count(AppointmentRow.id)
    visited_count = func.count(case((AppointmentRow.status == STATUS_ATTENDED, AppointmentRow.id)))
    # Department/doctor of the MOST RECENT appointment -- correlated scalar
    # subqueries (not a plain join) since the outer query is already grouped
    # to one row per patient and "which row is last_visit" can't be picked
    # out of a join without this. Both ordered identically so they always
    # name the same underlying appointment.
    _latest_appt = (
        select(AppointmentRow.id)
        .where(AppointmentRow.hospital_id == PatientRow.hospital_id, AppointmentRow.patient_id == PatientRow.id)
        .order_by(AppointmentRow.scheduled_at.desc())
        .limit(1)
        .correlate(PatientRow)
        .scalar_subquery()
    )
    last_visit_department = (
        select(Department.name)
        .select_from(AppointmentRow)
        .outerjoin(Department, Department.id == AppointmentRow.department_id)
        .where(AppointmentRow.id == _latest_appt)
        .correlate(PatientRow)
        .scalar_subquery()
    )
    last_visit_doctor = (
        select(DoctorRow.name)
        .select_from(AppointmentRow)
        .outerjoin(DoctorRow, DoctorRow.id == AppointmentRow.doctor_id)
        .where(AppointmentRow.id == _latest_appt)
        .correlate(PatientRow)
        .scalar_subquery()
    )
    stmt = (
        select(
            PatientRow.id, PatientRow.phone, PatientRow.name, PatientRow.patient_display_id, PatientRow.mrn,
            PatientRow.date_of_birth, PatientRow.gender, PatientRow.status, PatientRow.created_at,
            PatientRow.duplicate_of_patient_id, PatientRow.duplicate_flag_reason,
            last_visit.label("last_visit"), visit_count.label("visit_count"),
            visited_count.label("visited_count"), last_visit_department.label("department_name"),
            last_visit_doctor.label("doctor_name"),
        )
        .select_from(PatientRow)
        .outerjoin(
            AppointmentRow,
            and_(AppointmentRow.hospital_id == PatientRow.hospital_id, AppointmentRow.patient_id == PatientRow.id),
        )
        .where(PatientRow.hospital_id == hospital_id)
        .group_by(
            PatientRow.id, PatientRow.phone, PatientRow.name, PatientRow.patient_display_id, PatientRow.mrn,
            PatientRow.date_of_birth, PatientRow.gender, PatientRow.status, PatientRow.created_at,
            PatientRow.duplicate_of_patient_id, PatientRow.duplicate_flag_reason,
        )
        .order_by(last_visit.desc().nulls_last(), PatientRow.name.nulls_last(), PatientRow.phone)
    )
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(PatientRow.phone.ilike(like), PatientRow.name.ilike(like)))
    return stmt


def list_patients(hospital_id: int, search: str | None = None, limit: int = 200) -> list[dict]:
    """Full patient directory for /portal/patients -- unlike search_patients()
    (name/phone only, built for the new-booking form's autocomplete), this
    also surfaces last_visit/visit_count so staff can see engagement at a
    glance, and returns every patient (not just search hits) when no query
    is given."""
    session = get_session()
    search = (search or "").strip()
    rows = session.execute(_patients_with_visit_stats_stmt(hospital_id, search or None).limit(limit)).all()
    return [
        {
            "id": r.id, "phone": r.phone, "name": r.name, "patient_display_id": r.patient_display_id,
            "mrn": r.mrn, "last_visit": r.last_visit, "visit_count": r.visit_count,
            "visited_count": r.visited_count, "date_of_birth": r.date_of_birth, "gender": r.gender,
            "age": age_from_dob(r.date_of_birth), "status": r.status, "created_at": r.created_at,
            "department_name": r.department_name, "doctor_name": r.doctor_name,
            "duplicate_of_patient_id": r.duplicate_of_patient_id, "duplicate_flag_reason": r.duplicate_flag_reason,
        }
        for r in rows
    ]


def get_recent_patients(hospital_id: int, limit: int = 5) -> list[dict]:
    """The dashboard's small "Patients" widget -- same shape as list_patients()
    but capped short and always unfiltered (most-recently-seen patients),
    since it's a glance-and-click-through widget, not a search surface."""
    return list_patients(hospital_id, search=None, limit=limit)


def get_patients_for_doctor(hospital_id: int, doctor_id: str, limit: int = 500) -> list[dict]:
    """Doctor-portal follow-up: only patients this DOCTOR has actually seen,
    with last_visit/visit_count/visited_count scoped to appointments WITH
    THIS DOCTOR specifically -- deliberately not
    _patients_with_visit_stats_stmt() reused as-is, since that joins every
    appointment a patient has at the hospital (any doctor); "last visit"
    here must mean "last visit with me," not the patient's whole hospital
    history, and a patient who's never seen this doctor must not appear at
    all (an INNER join on doctor_id, not the shared helper's OUTER join).
    doctor_id comes from the caller's own verified token, never a request
    parameter -- same discipline as every other /api/doctor/* query."""
    last_visit = func.max(AppointmentRow.scheduled_at)
    visit_count = func.count(AppointmentRow.id)
    visited_count = func.count(case((AppointmentRow.status == STATUS_ATTENDED, AppointmentRow.id)))
    session = get_session()
    rows = session.execute(
        select(
            PatientRow.id, PatientRow.phone, PatientRow.name, PatientRow.patient_display_id, PatientRow.mrn,
            last_visit.label("last_visit"), visit_count.label("visit_count"),
            visited_count.label("visited_count"),
        )
        .select_from(PatientRow)
        .join(
            AppointmentRow,
            and_(
                AppointmentRow.hospital_id == PatientRow.hospital_id, AppointmentRow.patient_id == PatientRow.id,
                AppointmentRow.doctor_id == doctor_id,
            ),
        )
        .where(PatientRow.hospital_id == hospital_id)
        .group_by(PatientRow.id, PatientRow.phone, PatientRow.name, PatientRow.patient_display_id, PatientRow.mrn)
        .order_by(last_visit.desc().nulls_last(), PatientRow.name.nulls_last(), PatientRow.phone)
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id, "phone": r.phone, "name": r.name, "patient_display_id": r.patient_display_id,
            "mrn": r.mrn, "last_visit": r.last_visit, "visit_count": r.visit_count,
            "visited_count": r.visited_count,
        }
        for r in rows
    ]


# --- Patient records (Section 12.10: visit history, notes, documents) ---

_PATIENT_COLUMNS = (
    PatientRow.id, PatientRow.hospital_id, PatientRow.phone, PatientRow.name, PatientRow.date_of_birth,
    PatientRow.gender, PatientRow.address, PatientRow.patient_display_id, PatientRow.mrn,
    PatientRow.status, PatientRow.created_at,
)


def get_patient(hospital_id: int, patient_id: int) -> dict | None:
    """The single ownership check every patient-detail/notes/documents route
    uses before doing anything else -- returns None for a patient_id that
    doesn't exist OR belongs to a different hospital, so callers can't tell
    the two cases apart from the response (same 404-not-403 discipline as
    get_doctor_full())."""
    session = get_session()
    row = session.execute(select(*_PATIENT_COLUMNS).where(PatientRow.hospital_id == hospital_id, PatientRow.id == patient_id)).first()
    return dict(row._mapping) if row else None


def get_patient_by_phone(hospital_id: int, phone: str) -> dict | None:
    """Section 12.11: the WhatsApp booking flow's "have we met this patient
    before" check -- unlike get_patient() (looked up by the portal's own
    numeric id), the bot only ever knows a phone number. Exact match, not
    search_patients()'s partial ILIKE -- this is an identity lookup, not a
    staff-typed search box.

    Patient identity SEPARATION (Spec.md Section 0): kept as-is for callers
    that still want "the one patient this phone has" (e.g. "My Details",
    which predates multi-patient linking and is out of scope for this
    round) -- returns the FIRST matching row if a phone somehow has more
    than one `patients` row, not an error, but is no longer the right
    lookup for anything booking-related. Use get_active_patients_for_phone()
    for that."""
    session = get_session()
    row = session.execute(select(*_PATIENT_COLUMNS).where(PatientRow.hospital_id == hospital_id, PatientRow.phone == phone)).first()
    return dict(row._mapping) if row else None


# --- Patient identity SEPARATION (Spec.md Section 0): one WhatsApp phone can
# link up to platform_settings.max_active_patient_links patient profiles (a shared family phone).
# patient_links is the source of truth for phone<->patient associations;
# `patients` itself no longer implies "one row per phone" (db/schema.sql's own
# comment on the dropped UNIQUE(hospital_id, phone) constraint explains why). ---

# CareConnect architecture doc alignment (Spec.md Section 0), Section 17's
# fixed relationship enum -- single source of truth the WhatsApp picker and
# db/schema.sql's own patient_links_relationship_label_check CHECK
# constraint both mirror. Stored/displayed as Title Case, matching the
# migration backfill's own pre-existing 'Self' value -- not the doc's
# literal uppercase SELF/MOTHER/... strings, which would need a data
# migration for no functional gain (confirmed with the user).
RELATIONSHIP_OPTIONS = ("Self", "Mother", "Father", "Son", "Daughter", "Spouse", "Guardian", "Other")

# "Myself / Someone Else" registration step (flows/patient_identity.py) only
# ever writes one of these two RELATIONSHIP_OPTIONS values -- the other 6
# (Mother/Father/Son/Daughter/Spouse/Guardian) stay reachable at the data
# layer (e.g. a future richer picker, or portal-side manual editing) but are
# never written by the live chat flow today.
RELATIONSHIP_SELF = "Self"
RELATIONSHIP_OTHER = "Other"

# Chat-flow registration (flows/patient_identity.py) now collects this as a
# required step, but the column itself stays nullable at the DB level --
# other write paths (portal demographics edit, the pre-existing appointment-
# driven patient backfill in db/init_db.py) still legitimately leave/set it
# NULL, same reasoning as patient_links.relationship_label above.
GENDER_OPTIONS = ("Male", "Female", "Other")

# CareConnect architecture doc alignment, Section 18's Patient Master state
# model (CREATED collapsed into ACTIVE, confirmed with the user -- see
# db/schema.sql's own comment on patients.status for why).
PATIENT_STATUS_ACTIVE = "active"
PATIENT_STATUS_BLOCKED = "blocked"
PATIENT_STATUS_INACTIVE = "inactive"
PATIENT_STATUSES = (PATIENT_STATUS_ACTIVE, PATIENT_STATUS_BLOCKED, PATIENT_STATUS_INACTIVE)


def get_active_patients_for_phone(hospital_id: int, phone: str) -> list[dict]:
    """The real "which patients does this phone see" lookup, replacing
    get_patient_by_phone() for every WhatsApp-facing use -- returns every
    ACTIVE (unlinked_at IS NULL) linked patient, soonest-linked first (so the
    original/"Self" profile from before this feature existed, or the first
    family member added, is always first -- the natural single-result case
    for a still-single-patient phone stays first without any extra logic).

    CareConnect architecture doc alignment (Spec.md Section 0), Section 18:
    also filters to patients.status = 'active' -- a hospital-blocked or
    inactive patient is excluded from selection/auto-continue entirely,
    without touching their patient_links row at all (their link stays
    active; they just can't be chosen while the PATIENT record itself is
    blocked/inactive). This is the one enforcement point every WhatsApp-
    facing patient list goes through, same "filter at the one real read
    path" precedent doctors.is_active already established."""
    session = get_session()
    rows = session.execute(
        select(
            PatientRow.id, PatientRow.name, PatientRow.date_of_birth, PatientRow.patient_display_id,
            PatientLink.relationship_label, PatientLink.id.label("link_id"),
        )
        .select_from(PatientLink)
        .join(PatientRow, PatientRow.id == PatientLink.patient_id)
        .where(
            PatientLink.hospital_id == hospital_id, PatientLink.whatsapp_phone == phone,
            PatientLink.unlinked_at.is_(None), PatientRow.status == "active",
        )
        .order_by(PatientLink.linked_at, PatientLink.id)
    ).all()
    return [dict(r._mapping) for r in rows]


def validate_active_patient_link(hospital_id: int, phone: str, patient_id: int) -> bool:
    """CareConnect architecture doc alignment (Spec.md Section 0), Section
    14's patient-context validation: re-checked at the point of an actual
    patient-specific WRITE (booking/cancel/reschedule confirm), not just at
    selection time -- a link resolved several messages ago in a multi-step
    flow (department -> doctor -> date -> time -> confirm) could have been
    unlinked, or the patient blocked, in between. True only if the link is
    still active AND the patient itself is still 'active'."""
    session = get_session()
    row = session.execute(
        select(PatientLink.id)
        .select_from(PatientLink)
        .join(PatientRow, PatientRow.id == PatientLink.patient_id)
        .where(
            PatientLink.hospital_id == hospital_id, PatientLink.whatsapp_phone == phone,
            PatientLink.patient_id == patient_id, PatientLink.unlinked_at.is_(None), PatientRow.status == "active",
        )
    ).first()
    return row is not None


def count_active_links_for_phone(hospital_id: int, phone: str) -> int:
    session = get_session()
    return session.execute(
        select(func.count(PatientLink.id)).where(
            PatientLink.hospital_id == hospital_id, PatientLink.whatsapp_phone == phone, PatientLink.unlinked_at.is_(None),
        )
    ).scalar_one()


def has_self_linked_patient(hospital_id: int, care_connect_account_id: int) -> bool:
    """"Myself / Someone Else" registration step (flows/patient_identity.py):
    soft pre-check, called BEFORE showing the Myself/Someone Else buttons --
    once an account has one, every later "Add Patient" for that account at
    this hospital skips straight to "Someone Else" (never asked twice). The
    hard, race-safe version of this same rule lives in
    _link_patient_under_cap() below, under the same advisory lock as the
    active-links-cap check."""
    session = get_session()
    row = session.execute(
        select(PatientLink.id).where(
            PatientLink.hospital_id == hospital_id, PatientLink.care_connect_account_id == care_connect_account_id,
            PatientLink.relationship_label == RELATIONSHIP_SELF, PatientLink.unlinked_at.is_(None),
        )
    ).first()
    return row is not None


def _check_relationship_label(relationship_label: str | None) -> None:
    if relationship_label is not None and relationship_label not in RELATIONSHIP_OPTIONS:
        raise ValueError(f"relationship_label must be one of {RELATIONSHIP_OPTIONS} or None, got {relationship_label!r}")


def _check_gender(gender: str | None) -> None:
    if gender is not None and gender not in GENDER_OPTIONS:
        raise ValueError(f"gender must be one of {GENDER_OPTIONS} or None, got {gender!r}")


def _link_patient_under_cap(conn, hospital_id: int, phone: str, patient_id: int, relationship_label: str | None) -> None:
    """Shared by create_patient_profile()/link_existing_patient(): the
    advisory-locked "count active links, raise if at cap, else INSERT the
    link row" sequence -- must run under `conn`'s already-open transaction
    (both callers wrap this in BEGIN/COMMIT/ROLLBACK) so the count-then-
    insert is atomic against a genuine concurrent "add/link a patient" tap
    from the same identity.

    The cap and its lock are keyed on care_connect_account_id (the durable
    global identity -- db/schema.sql's own comment on care_connect_accounts),
    not the raw phone string, so the account is resolved FIRST, before the
    lock/count. In the common case this is identical to keying on phone,
    since one WhatsApp number is 1:1 with one account today -- but the
    account is the correct identity to enforce "up to
    platform_settings.max_active_patient_links per hospital" against, robust to a person's
    phone number changing while their account persists (or the reverse).
    Still per-hospital: a second hospital's own patient_links rows are
    counted separately, same as before.

    Deliberately NOT migrated to get_session()/ORM, permanently, along with
    create_patient_profile()/link_existing_patient() below: pg_advisory_xact_lock
    is TRANSACTION-scoped (auto-released at COMMIT/ROLLBACK, unlike the
    session-scoped pg_advisory_lock/unlock pair elsewhere) -- it only
    provides real protection inside an actual multi-statement BEGIN/COMMIT
    block, which is exactly what these three functions build via manual
    "BEGIN"/"COMMIT"/"ROLLBACK" text statements on a single raw connection.
    The ORM engine runs in AUTOCOMMIT (see db/connection.py's get_engine()),
    so every session.execute() is its own independent transaction by
    design -- an advisory lock taken and immediately released within one
    autocommitted statement provides no protection at all against a second,
    genuinely concurrent call racing in between statements. This is exactly
    the class of concurrency-critical code the migration plan's own
    guarantee calls out to leave untouched permanently, alongside the
    atomic reference/display-id counters (db/models.py). Also still calls
    _get_or_create_account_in_conn() (accounts.py's raw-conn helper) inside
    this same transaction -- see consent.py's docstring for that
    dependency's own status."""
    # CareConnect account/identity layer: resolved up front (not just for the
    # INSERT below) since the cap/lock now key on it, not on `phone`. Cheap
    # on the common path where webhook/dispatch.py's own per-message
    # identify_contact() call already created the account earlier in this
    # same conversation.
    account = _get_or_create_account_in_conn(conn, phone, phone_number=phone)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"patient_links|{hospital_id}|{account['id']}",))
    active_count = conn.execute(
        "SELECT COUNT(*) AS c FROM patient_links WHERE hospital_id = ? AND care_connect_account_id = ? AND unlinked_at IS NULL",
        (hospital_id, account["id"]),
    ).fetchone()["c"]
    # Global platform-admin-editable cap (db/repositories/platform_settings.py),
    # NOT a per-hospital value -- read fresh here rather than a cached/
    # imported constant, since a platform admin can change it at any time.
    max_active_patient_links = conn.execute(
        "SELECT max_active_patient_links FROM platform_settings WHERE id = 1"
    ).fetchone()["max_active_patient_links"]
    if active_count >= max_active_patient_links:
        raise TooManyLinkedPatientsError(
            f"This phone number already has {max_active_patient_links} linked patients -- unlink one first."
        )
    if relationship_label == RELATIONSHIP_SELF:
        # Hard, race-safe backstop under the SAME advisory lock as the cap
        # check above -- the chat flow already soft-checks this via
        # has_self_linked_patient() before ever asking Myself/Someone Else,
        # so this should only ever fire against a genuinely concurrent
        # second "Myself" tap from the same account.
        existing_self = conn.execute(
            "SELECT 1 FROM patient_links WHERE hospital_id = ? AND care_connect_account_id = ? "
            "AND relationship_label = ? AND unlinked_at IS NULL",
            (hospital_id, account["id"], RELATIONSHIP_SELF),
        ).fetchone()
        if existing_self is not None:
            raise DuplicateSelfLinkError("This account already has a 'Myself' patient linked at this hospital.")
    conn.execute(
        "INSERT INTO patient_links (hospital_id, whatsapp_phone, patient_id, relationship_label, care_connect_account_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (hospital_id, phone, patient_id, relationship_label, account["id"]),
    )


def find_potential_duplicate_patient(
    hospital_id: int, name: str, contact_phone: str, date_of_birth: str, gender: str,
) -> dict | None:
    """CareConnect architecture doc alignment (Spec.md Section 0), Sections
    8-10: searched BEFORE create_patient_profile() creates a brand-new
    `patients` row/MRN, so a family member who already has a hospital
    record (e.g. from a staff-created booking, a different WhatsApp number,
    or the SAME phone re-adding someone already in their own list) isn't
    silently duplicated. Matching criteria (confirmed with the user,
    widened from name+contact only): exact name (case/whitespace-
    insensitive) AND exact contact phone number (patients.phone -- the
    patient's OWN number: the messaging phone for "Myself", the collected
    number for "Someone Else", see create_patient_profile()'s contact_phone
    param) AND exact date of birth AND exact gender, among this hospital's
    ACTIVE patients -- all four together make a match essentially certain
    to be the same real person.

    date_of_birth/gender are REQUIRED (not Optional) rather than compared
    with a None-tolerant `is_()`/`==` branch: SQLAlchemy compiles
    `Column == None` to `IS NULL`, so an Optional param would silently
    match every candidate with an unrecorded date_of_birth/gender -- a real
    false-positive risk. The one real caller (registration) always has both
    collected by the time it calls this, so requiring them closes that path
    structurally. The reverse asymmetry is accepted as-is: an EXISTING row
    with a NULL date_of_birth/gender (e.g. staff-created before gender was
    mandatory) will correctly never match a concrete incoming value
    (three-valued SQL logic) -- no false positive, but a genuine duplicate
    could be missed; that's a known limitation, not a bug to fix here.

    Deliberately does NOT exclude a patient already linked to the caller's
    own phone (an earlier version of this function did, via a
    "not already actively linked to this phone" clause) -- that exclusion
    let the exact same name+contact be re-added from the same phone
    indefinitely, silently creating a genuine duplicate `patients` row every
    time, since the real existing record was filtered out of its own
    duplicate search. The caller (flows/patient_identity.py) now checks
    validate_active_patient_link() itself on the returned match to tell
    "already yours" apart from "exists elsewhere" and respond accordingly.
    Returns the first match (deterministic: lowest patient id) or None."""
    session = get_session()
    row = session.execute(
        select(PatientRow.id, PatientRow.name, PatientRow.date_of_birth, PatientRow.patient_display_id, PatientRow.phone)
        .where(
            PatientRow.hospital_id == hospital_id, PatientRow.status == "active",
            func.lower(func.trim(PatientRow.name)) == func.lower(func.trim(name)),
            PatientRow.phone == contact_phone,
            PatientRow.date_of_birth == date_of_birth, PatientRow.gender == gender,
        )
        .order_by(PatientRow.id)
        .limit(1)
    ).first()
    return dict(row._mapping) if row else None


# Which of the 4 identity fields this hospital's staff see named in a flag
# reason -- kept in this fixed order so "matches on X, Y, Z" always reads
# name -> phone -> date of birth -> gender, not whatever order a dict
# happened to iterate in.
_DUPLICATE_FLAG_FIELDS = ("name", "phone", "date of birth", "gender")


def _flag_duplicate_if_matches(
    conn, hospital_id: int, patient_id: int, name: str | None, phone: str | None,
    date_of_birth: str | None, gender: str | None,
) -> None:
    """Patients page follow-up (Section 0): call this right after a BRAND-NEW
    `patients` row is inserted (both _upsert_patient()'s fresh-INSERT branch,
    db/repositories/appointments.py, and create_patient_profile() below) --
    never on an update to an existing row, which already has its own settled
    identity. Scans this hospital's other ACTIVE patients (same status scope
    find_potential_duplicate_patient() above uses) for one matching the new
    row on at least 3 of {name, phone, date_of_birth, gender}; if found,
    stamps duplicate_of_patient_id/duplicate_flag_reason on the NEW row so
    staff can review it on the patient list -- purely informational, no
    merge/dismiss action yet.

    Unlike find_potential_duplicate_patient()'s all-4-exact check (which
    runs BEFORE creation, to offer linking instead), this is a softer,
    after-the-fact signal: a field only counts as matching, or as a
    genuinely differing field named in the reason, when BOTH sides have a
    real value for it -- missing data on either side is simply excluded
    from the comparison (same None-tolerance reasoning as that function's
    own docstring), so an incomplete profile can't itself manufacture a
    false flag. name/gender compare case/whitespace-insensitively; phone/
    date_of_birth compare exactly. When more than one existing patient
    reaches the 3-of-4 threshold, the one matching the MOST fields wins
    (ties broken by lowest id, i.e. the oldest record)."""
    if not phone and not name and not date_of_birth and not gender:
        return
    rows = conn.execute(
        "SELECT id, name, phone, date_of_birth, gender, patient_display_id FROM patients "
        "WHERE hospital_id = ? AND status = 'active' AND id != ? ORDER BY id",
        (hospital_id, patient_id),
    ).fetchall()
    if not rows:
        return

    def _norm(value: str | None) -> str | None:
        return (value or "").strip().lower() or None

    mine = (_norm(name), phone or None, date_of_birth or None, _norm(gender))
    best: tuple[int, list[str], list[str], object] | None = None
    for row in rows:
        theirs = (_norm(row["name"]), row["phone"] or None, row["date_of_birth"] or None, _norm(row["gender"]))
        matched, differing = [], []
        for label, mine_value, their_value in zip(_DUPLICATE_FLAG_FIELDS, mine, theirs):
            if mine_value is None or their_value is None:
                continue
            (matched if mine_value == their_value else differing).append(label)
        if len(matched) >= 3 and (best is None or len(matched) > best[0]):
            best = (len(matched), matched, differing, row)
    if best is None:
        return
    _, matched, differing, row = best
    reason = f"Matches patient {row['patient_display_id'] or row['id']} on {', '.join(matched)}"
    if differing:
        reason += f" -- {', '.join(differing)} differs"
    conn.execute(
        "UPDATE patients SET duplicate_of_patient_id = ?, duplicate_flag_reason = ? WHERE id = ?",
        (row["id"], reason, patient_id),
    )


def create_patient_profile(
    hospital_id: int, phone: str, name: str, date_of_birth: str | None, relationship_label: str | None = None,
    gender: str | None = None, contact_phone: str | None = None,
) -> dict:
    """Creates a brand-new `patients` row (NEVER an upsert-by-phone -- multiple
    profiles are the whole point now) and links it to `phone` via a new
    patient_links row. Raises TooManyLinkedPatientsError if this phone already
    has MAX_ACTIVE_PATIENT_LINKS active links, DuplicateSelfLinkError if
    relationship_label="Self" and this account already has one at this
    hospital, or ValueError if relationship_label isn't one of
    RELATIONSHIP_OPTIONS (or None), or gender isn't one of GENDER_OPTIONS
    (or None).

    `contact_phone` is the patient's OWN number, stored as `patients.phone`
    -- distinct from `phone`, which is the WhatsApp number this conversation
    is happening on and always becomes `patient_links.whatsapp_phone`
    regardless of who the patient is. Defaults to `phone` when not given (the
    "Myself" case, and every pre-existing caller that never collected a
    separate contact number) -- flows/patient_identity.py passes the two
    apart only for a "Someone Else" registration, where the family member's
    own number was explicitly asked for and can legitimately differ from the
    messaging phone.

    `gender` stays Optional here (like relationship_label) so other callers
    (dead-code core/booking_flow.py path, direct test fixtures) that never
    collected it keep working unchanged -- it's flows/patient_identity.py's
    own live registration flow that makes it effectively required, by never
    reaching this call until a valid gender has been picked.

    Callers are expected to have already checked
    find_potential_duplicate_patient() and confirmed with the patient that
    this is genuinely a NEW patient, not an existing one to link instead
    (link_existing_patient(), below) -- this function itself does not
    re-check for a duplicate, so it can also serve any future caller (e.g.
    a staff-side "register new patient" form) that has already resolved
    identity its own way.

    Wrapped in a real BEGIN/COMMIT/ROLLBACK block -- see
    _link_patient_under_cap()'s own docstring for the advisory-lock
    reasoning, unchanged from before this was extracted into a shared
    helper."""
    _check_relationship_label(relationship_label)
    _check_gender(gender)
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        patient_row = conn.execute(
            "INSERT INTO patients (hospital_id, phone, name, date_of_birth, gender) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (hospital_id, contact_phone or phone, name, date_of_birth, gender),
        ).fetchone()
        assert patient_row is not None  # INSERT ... RETURNING always returns the inserted row
        patient_id = patient_row["id"]
        display_id, mrn = _generate_patient_identifiers(conn, hospital_id)
        conn.execute(
            "UPDATE patients SET patient_display_id = ?, mrn = ? WHERE id = ?", (display_id, mrn, patient_id),
        )
        _link_patient_under_cap(conn, hospital_id, phone, patient_id, relationship_label)
        _flag_duplicate_if_matches(conn, hospital_id, patient_id, name, contact_phone or phone, date_of_birth, gender)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {
        "id": patient_id, "name": name, "date_of_birth": date_of_birth, "gender": gender,
        "patient_display_id": display_id, "mrn": mrn,
        "relationship_label": relationship_label,
    }


def link_existing_patient(
    hospital_id: int, phone: str, patient_id: int, relationship_label: str | None = None,
) -> dict:
    """CareConnect architecture doc alignment (Spec.md Section 0), Section
    9: the "Link Existing Patient" choice after find_potential_duplicate_patient()
    surfaces a plausible match -- creates a new patient_links row pointing
    at the EXISTING `patients` row (no new patient/MRN, matching Section 9's
    explicit "should not create a second MRN merely because..." rule)
    rather than create_patient_profile()'s "always a fresh row." Subject to
    the exact same 5-active-link cap as a new profile. Raises ValueError if
    patient_id doesn't belong to hospital_id, or isn't 'active'."""
    _check_relationship_label(relationship_label)
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        patient_row = conn.execute(
            "SELECT id, name, date_of_birth, patient_display_id FROM patients WHERE hospital_id = ? AND id = ? AND status = ?",
            (hospital_id, patient_id, PATIENT_STATUS_ACTIVE),
        ).fetchone()
        if patient_row is None:
            raise ValueError(f"patient_id {patient_id} not found (or not active) for hospital {hospital_id}")
        _link_patient_under_cap(conn, hospital_id, phone, patient_id, relationship_label)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {
        "id": patient_row["id"], "name": patient_row["name"], "date_of_birth": patient_row["date_of_birth"],
        "patient_display_id": patient_row["patient_display_id"], "relationship_label": relationship_label,
    }


def set_patient_status(hospital_id: int, patient_id: int, status: str) -> dict | None:
    """Staff-side action (the portal's patient detail page) for Section 18's
    BLOCKED/INACTIVE states -- a hospital-level fact about the PATIENT
    record, deliberately independent of patient_links (blocking a patient
    doesn't touch or require touching any phone's link to them; see
    get_active_patients_for_phone()'s own docstring). Returns None if
    patient_id doesn't belong to this hospital, or raises ValueError for an
    unrecognized status."""
    if status not in PATIENT_STATUSES:
        raise ValueError(f"status must be one of {PATIENT_STATUSES}, got {status!r}")
    session = get_session()
    result = cast(CursorResult, session.execute(
        update(PatientRow).where(PatientRow.hospital_id == hospital_id, PatientRow.id == patient_id).values(status=status)
    ))
    session.commit()
    if result.rowcount == 0:
        return None
    return get_patient(hospital_id, patient_id)


def unlink_patient(hospital_id: int, phone: str, patient_id: int) -> bool:
    """Soft-unlink only -- sets unlinked_at on the matching ACTIVE link row.
    Never touches `patients` or `appointments`: a soft-unlink is purely a
    patient_links row update, so this patient's appointment history and
    Patient ID are completely unaffected (confirmed by design, not just by
    accident of implementation -- there is no code path here that could
    touch either table). Returns False if no such active link exists (stale
    tap, already unlinked, or a patient_id belonging to a different phone)."""
    session = get_session()
    result = cast(CursorResult, session.execute(
        update(PatientLink)
        .where(
            PatientLink.hospital_id == hospital_id, PatientLink.whatsapp_phone == phone,
            PatientLink.patient_id == patient_id, PatientLink.unlinked_at.is_(None),
        )
        .values(unlinked_at=datetime.now().isoformat())
    ))
    session.commit()
    return result.rowcount > 0


def get_patient_link_consent(hospital_id: int, phone: str, patient_id: int) -> dict | None:
    """CareConnect architecture doc alignment (Spec.md Section 0), Section
    20's Consent & Privacy menu item -- reads the active link's own
    service_consent/marketing_consent columns. Returns None if there's no
    active link for this (hospital_id, phone, patient_id) triple."""
    session = get_session()
    row = session.execute(
        select(PatientLink.service_consent, PatientLink.marketing_consent).where(
            PatientLink.hospital_id == hospital_id, PatientLink.whatsapp_phone == phone,
            PatientLink.patient_id == patient_id, PatientLink.unlinked_at.is_(None),
        )
    ).first()
    return dict(row._mapping) if row else None


def set_marketing_consent(hospital_id: int, phone: str, patient_id: int, consented: bool) -> bool:
    """The one genuinely user-togglable consent flag (Section 20) --
    service_consent is implicit in having an active link at all (see
    db/schema.sql's own comment on why it's not a separate WhatsApp-facing
    toggle); marketing_consent is independent, opt-in, and freely
    reversible either direction. Returns False if there's no active link to
    update."""
    session = get_session()
    result = cast(CursorResult, session.execute(
        update(PatientLink)
        .where(
            PatientLink.hospital_id == hospital_id, PatientLink.whatsapp_phone == phone,
            PatientLink.patient_id == patient_id, PatientLink.unlinked_at.is_(None),
        )
        .values(marketing_consent=consented)
    ))
    session.commit()
    return result.rowcount > 0


def delete_patient_hard(hospital_id: int, patient_id: int) -> bool:
    """DEV/TESTING ONLY -- irreversibly removes the patient row plus every
    row that references it (visit notes, documents, links). Confirmed with
    the user: a still-'booked' (upcoming) appointment is permanently
    deleted outright, not just detached -- there's no patient left to show
    up for it, and hard-deleting the row (rather than soft-deleting, which
    stays restricted to already-resolved appointments elsewhere in this
    app) also frees the doctor's slot for rebooking immediately. Every
    OTHER appointment (cancelled/rescheduled/attended/no_show -- already-
    resolved history) is still only detached (patient_id set NULL, row
    kept), same as before, so that history survives the patient itself
    being removed. Swap the portal route to delete_patient_soft() before
    this goes to production -- see that function's docstring."""
    session = get_session()
    upcoming_ids = [
        row.id for row in session.execute(
            select(AppointmentRow.id).where(
                AppointmentRow.hospital_id == hospital_id, AppointmentRow.patient_id == patient_id,
                AppointmentRow.status == STATUS_BOOKED,
            )
        ).all()
    ]
    # Visit notes/documents are deleted (by patient_id, below) before the
    # appointment rows themselves so nothing is left referencing a
    # about-to-be-removed appointment_id when the FK is checked.
    session.execute(delete(PatientVisitNote).where(PatientVisitNote.hospital_id == hospital_id, PatientVisitNote.patient_id == patient_id))
    session.execute(delete(PatientDocument).where(PatientDocument.hospital_id == hospital_id, PatientDocument.patient_id == patient_id))
    if upcoming_ids:
        session.execute(delete(AppointmentReminder).where(AppointmentReminder.appointment_id.in_(upcoming_ids)))
        session.execute(delete(AppointmentRow).where(AppointmentRow.id.in_(upcoming_ids)))
    session.execute(
        update(AppointmentRow)
        .where(AppointmentRow.hospital_id == hospital_id, AppointmentRow.patient_id == patient_id)
        .values(patient_id=None)
    )
    session.execute(delete(PatientLink).where(PatientLink.hospital_id == hospital_id, PatientLink.patient_id == patient_id))
    result = cast(CursorResult, session.execute(
        delete(PatientRow).where(PatientRow.hospital_id == hospital_id, PatientRow.id == patient_id)
    ))
    session.commit()
    return result.rowcount > 0


def delete_patient_soft(hospital_id: int, patient_id: int) -> dict | None:
    """Production-safe alternative to delete_patient_hard() -- reuses the
    existing PATIENT_STATUSES model (Section 18) instead of removing any
    row, so appointment/visit history is preserved. Swap the portal route
    to this once the app is out of dev/testing."""
    return set_patient_status(hospital_id, patient_id, PATIENT_STATUS_INACTIVE)


def update_patient_demographics(
    hospital_id: int, patient_id: int, date_of_birth: str | None, gender: str | None, address: str | None,
) -> dict | None:
    """All three fields optional -- an empty-string/None value clears that
    field rather than being rejected, since none of this was ever required
    at patient creation and staff filling it in gradually is the expected
    path, not an all-at-once form."""
    session = get_session()
    result = cast(CursorResult, session.execute(
        update(PatientRow)
        .where(PatientRow.hospital_id == hospital_id, PatientRow.id == patient_id)
        .values(date_of_birth=date_of_birth or None, gender=gender or None, address=address or None)
    ))
    session.commit()
    if result.rowcount == 0:
        return None
    return get_patient(hospital_id, patient_id)


