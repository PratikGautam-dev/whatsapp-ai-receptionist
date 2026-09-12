# tests/test_procedure_flows.py
"""Daycare/Procedure rebuild: replaces the old duration-picker flow entirely.
Covers the instant-booking path (Step 1 catalog -> date/time -> confirm),
the approval-required path (request -> portal approve -> resume -> confirm,
and reject), the multi-resource-constraint availability engine, and "Request
Reschedule". The old daycare_duration_options-based tests this file replaces
were removed from tests/test_booking_flow.py."""
import os
from datetime import datetime, timedelta

import pytest

import db.repository as db
from connectors import Tier1Connector
from core.session_store import InMemorySessionStore
from flows.booking import handle_incoming

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "test@calendar")
os.environ.setdefault("GOOGLE_CALENDAR_OWNER_EMAIL", "test@test.com")
os.environ.setdefault("PORTAL_SECRET", "test-portal-secret")

from main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

PHONE = "+15550002222"


class FakeWhatsAppClient:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, text):
        self.sent.append(("text", {"to": to, "text": text}))

    async def send_buttons(self, to, body_text, buttons):
        self.sent.append(("buttons", {"to": to, "body_text": body_text, "buttons": buttons}))

    async def send_list(self, to, body_text, button_text, sections):
        self.sent.append(("list", {"to": to, "body_text": body_text, "button_text": button_text, "sections": sections}))


def tap(row_id: str) -> dict:
    return {"type": "interactive_reply", "id": row_id}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


@pytest.fixture
def sessions():
    return InMemorySessionStore()


def _procedures(hospital_id):
    return db.get_procedures(hospital_id)


def _create_instant_procedure(hospital_id, duration_minutes=30, resource_types=("staff",)):
    """A staff-only instant procedure with one staff resource covering it --
    the minimal shape needed to actually be bookable (same "not available
    until a resource is linked" discipline Diagnostic/Lab already enforce)."""
    procedure = db.create_procedure(hospital_id, "injection", "Test Injection", "instant", duration_minutes)
    db.set_required_resource_types(hospital_id, procedure["id"], list(resource_types))
    for rt in resource_types:
        db.create_procedure_resource(
            hospital_id, rt, f"{rt.title()} Pool A",
            working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], working_hours=["09:00-17:00"],
            slot_duration_minutes=30,
        )
    return db.get_procedure(hospital_id, procedure["id"])


def _create_approval_procedure(hospital_id, resource_types=("bed_chair", "staff")):
    procedure = db.create_procedure(hospital_id, "chemotherapy", "Test Chemo", "approval_required", 180)
    db.set_required_resource_types(hospital_id, procedure["id"], list(resource_types))
    for rt in resource_types:
        db.create_procedure_resource(
            hospital_id, rt, f"{rt.title()} Pool A",
            working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], working_hours=["09:00-17:00"],
            slot_duration_minutes=30,
        )
    return db.get_procedure(hospital_id, procedure["id"])


async def _start_procedure_booking(wa, sessions, hospital_id, phone: str = PHONE):
    sessions.set(hospital_id, phone, "AWAITING_APPOINTMENT_TYPE", {"patient_name": "Abhi Sharma", "patient_date_of_birth": 41})
    await handle_incoming(wa, sessions, phone, hospital_id, tap("daycare"))


def _login(password: str) -> str:
    resp = client.post("/api/portal/login", json={"password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _set_hospital_password(hosp_id: int, password: str) -> None:
    h = db.get_hospital(hosp_id)
    db.update_hospital(
        hosp_id, name=h.name, whatsapp_phone_number_id=h.whatsapp_phone_number_id, access_token=h.access_token,
        app_secret=h.app_secret, timezone=h.timezone, welcome_message_text=h.welcome_message_text,
        reminder_offsets_hours=h.reminder_offsets_hours, reminder_template_name=h.reminder_template_name,
        data_tier=h.data_tier, external_api_base_url=h.external_api_base_url,
        external_api_key=h.external_api_key, portal_password_hash=db.hash_portal_password(password),
        enabled_features=h.enabled_features,
    )


# --- Catalog rebuild: registry/type-flow wiring ---

def test_daycare_flow_has_no_department_step():
    from flows.booking.types.registry import get_type_flow
    flow = get_type_flow("daycare")
    assert flow.steps == ("AWAITING_PROCEDURE",)
    assert flow.on_selected is not None


# --- Instant booking: Step 1 -> date/time -> confirm ---

@pytest.mark.asyncio
async def test_instant_procedure_books_straight_through(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    procedure = _create_instant_procedure(hospital_id)

    await _start_procedure_booking(wa, sessions, hospital_id)
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_PROCEDURE"

    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(str(procedure["id"])))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_DATE"

    slots = db.get_procedure_available_slots(hospital_id, procedure["id"])
    assert slots  # a linked staff resource must produce real slots
    date_str = slots[0]["date"]
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(date_str))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_TIME_SLOT"
    slot = next(s for s in slots if s["date"] == date_str)
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(slot["id"]))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_CONFIRMATION"

    kind, kwargs = wa.sent[-1]
    assert kind == "buttons"
    assert procedure["name"] in kwargs["body_text"]

    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))
    kind, kwargs = wa.sent[-1]
    assert kind == "buttons"
    assert "daycare / procedure booking confirmed" in kwargs["body_text"].lower()
    assert procedure["name"] in kwargs["body_text"]

    appt = next(a for a in db.get_upcoming_appointments(hospital_id, offset_hours=999999) if a.phone == PHONE)
    assert appt.procedure_id == procedure["id"]
    assert appt.procedure_status == "CONFIRMED"
    assert appt.status == "booked"
    resources = db.get_procedure_resources_for_appointment(hospital_id, appt.id)
    assert len(resources) == 1
    assert resources[0]["resource_type"] == "staff"


@pytest.mark.asyncio
async def test_procedure_with_no_linked_resource_is_not_available(hospital_id, sessions):
    """Same "not available until a resource is linked" discipline
    Diagnostic/Lab already enforce -- no any-doctor-style fallback."""
    wa = FakeWhatsAppClient()
    procedure = db.create_procedure(hospital_id, "injection", "Unlinked Injection", "instant", 15)
    db.set_required_resource_types(hospital_id, procedure["id"], ["staff"])

    await _start_procedure_booking(wa, sessions, hospital_id)
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(str(procedure["id"])))

    assert sessions.get(hospital_id, PHONE)["state"] == "IDLE"
    kind, kwargs = wa.sent[-1]
    assert kind == "text" or kind == "list"


@pytest.mark.asyncio
async def test_video_link_and_lab_status_absent_from_procedure_confirmation(hospital_id, sessions):
    """The one thing that must not regress: a procedure booking's
    confirmation is exactly the spec's own shape, no stray fields from other
    types' own on_booking_confirmed hooks."""
    wa = FakeWhatsAppClient()
    procedure = _create_instant_procedure(hospital_id)
    await _start_procedure_booking(wa, sessions, hospital_id)
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(str(procedure["id"])))
    slots = db.get_procedure_available_slots(hospital_id, procedure["id"])
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(slots[0]["date"]))
    slot = next(s for s in slots if s["date"] == slots[0]["date"])
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(slot["id"]))
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))
    kind, kwargs = wa.sent[-1]
    assert "🎥" not in kwargs["body_text"]
    assert "meet.jit.si" not in kwargs["body_text"]


# --- Multi-resource-constraint availability ---

def test_one_empty_required_pool_blocks_the_whole_procedure(hospital_id):
    procedure = db.create_procedure(hospital_id, "chemotherapy", "Needs Two Pools", "instant", 60)
    db.set_required_resource_types(hospital_id, procedure["id"], ["bed_chair", "equipment"])
    db.create_procedure_resource(
        hospital_id, "bed_chair", "Bed 1", working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
        working_hours=["09:00-17:00"], slot_duration_minutes=30,
    )
    # No "equipment" resource configured at all.
    assert db.get_procedure_available_slots(hospital_id, procedure["id"]) == []


def test_duration_spanning_multiple_grid_slots_only_offers_fully_free_spans(hospital_id):
    """A 90-minute procedure on a resource with a 30-minute grid needs 3
    consecutive free sub-slots -- booking one span must remove all 3 from
    future availability for that resource."""
    procedure = db.create_procedure(hospital_id, "infusion_therapy", "Long Infusion", "instant", 90)
    db.set_required_resource_types(hospital_id, procedure["id"], ["staff"])
    db.create_procedure_resource(
        hospital_id, "staff", "Nurse Line A", working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
        working_hours=["09:00-17:00"], slot_duration_minutes=30, max_bookings_per_slot=1,
    )
    slots_before = db.get_procedure_available_slots(hospital_id, procedure["id"])
    assert slots_before
    first = slots_before[0]
    scheduled_at = datetime.fromisoformat(first["id"])
    db.create_procedure_appointment(hospital_id, PHONE, procedure["id"], scheduled_at, patient_name="Test", patient_date_of_birth=30)
    slots_after = db.get_procedure_available_slots(hospital_id, procedure["id"])
    assert first["id"] not in {s["id"] for s in slots_after}


def test_reserve_procedure_resources_raises_on_no_free_resource(hospital_id):
    from db.connection import IntegrityError, get_connection
    from db.repositories.procedure_slots import reserve_procedure_resources

    procedure = db.create_procedure(hospital_id, "injection", "Solo Staff", "instant", 15)
    db.set_required_resource_types(hospital_id, procedure["id"], ["staff"])
    conn = get_connection()
    with pytest.raises(IntegrityError):
        reserve_procedure_resources(hospital_id, procedure["id"], datetime.now() + timedelta(days=1), conn)


# --- Approval-required: request -> submit -> approve -> resume -> confirm ---

@pytest.mark.asyncio
async def test_approval_required_procedure_creates_request_not_a_slot(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    procedure = _create_approval_procedure(hospital_id)

    await _start_procedure_booking(wa, sessions, hospital_id)
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(str(procedure["id"])))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_PROCEDURE_REQUEST_CONFIRM"
    kind, kwargs = wa.sent[-1]
    assert kind == "buttons"
    assert procedure["name"] in kwargs["body_text"]

    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))
    kind, kwargs = wa.sent[-1]
    assert kind == "text"
    assert "sent to the hospital for verification" in kwargs["text"].lower()
    assert sessions.get(hospital_id, PHONE)["state"] == "IDLE"

    requested = db.get_pending_procedure_request(hospital_id, PHONE, procedure["id"])
    assert requested is not None
    assert requested.procedure_status == "REQUESTED"
    assert requested.status == "booked"  # base status placeholder, per lab_status precedent


@pytest.mark.asyncio
async def test_approved_request_notifies_and_resumes_to_date_selection(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    procedure = _create_approval_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")

    await _start_procedure_booking(wa, sessions, hospital_id)
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(str(procedure["id"])))
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))
    requested = db.get_pending_procedure_request(hospital_id, PHONE, procedure["id"])

    token = _login("adminpass")
    resp = client.post(f"/api/portal/bookings/{requested.id}/procedure/approve", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["appointment"]["procedure_status"] == "APPROVED"

    # Patient re-picks the SAME procedure -- resumes the approved request
    # straight into date selection instead of creating a duplicate.
    wa2 = FakeWhatsAppClient()
    sessions2 = InMemorySessionStore()
    await _start_procedure_booking(wa2, sessions2, hospital_id)
    await handle_incoming(wa2, sessions2, PHONE, hospital_id, tap(str(procedure["id"])))
    assert sessions2.get(hospital_id, PHONE)["state"] == "AWAITING_DATE"
    assert sessions2.get(hospital_id, PHONE)["context"]["_procedure_appointment_id"] == requested.id

    slots = db.get_procedure_available_slots(hospital_id, procedure["id"])
    await handle_incoming(wa2, sessions2, PHONE, hospital_id, tap(slots[0]["date"]))
    slot = next(s for s in slots if s["date"] == slots[0]["date"])
    await handle_incoming(wa2, sessions2, PHONE, hospital_id, tap(slot["id"]))
    await handle_incoming(wa2, sessions2, PHONE, hospital_id, tap("confirm"))

    updated = db.get_appointment(hospital_id, requested.id)
    assert updated.procedure_status == "CONFIRMED"
    assert updated.scheduled_at.isoformat() == slot["id"]
    # Confirming in place -- no second appointment row created.
    all_appts = [a for a in db.get_upcoming_appointments(hospital_id, offset_hours=999999) if a.phone == PHONE]
    assert len(all_appts) == 1


@pytest.mark.asyncio
async def test_rejected_request_notifies_patient(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    procedure = _create_approval_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")

    await _start_procedure_booking(wa, sessions, hospital_id)
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(str(procedure["id"])))
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))
    requested = db.get_pending_procedure_request(hospital_id, PHONE, procedure["id"])

    token = _login("adminpass")
    resp = client.post(
        f"/api/portal/bookings/{requested.id}/procedure/reject", headers=_auth(token), json={"reason": "No slots this month"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["appointment"]["procedure_status"] == "REJECTED"

    # Re-approving/re-rejecting an already-resolved request is rejected, not
    # a silent overwrite.
    resp2 = client.post(f"/api/portal/bookings/{requested.id}/procedure/approve", headers=_auth(token))
    assert resp2.status_code == 404


def test_approval_queue_lists_only_open_requests(hospital_id):
    procedure = _create_approval_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")
    requested = db.create_procedure_request(hospital_id, PHONE, procedure["id"], patient_name="Test", patient_date_of_birth=30)

    token = _login("adminpass")
    resp = client.get("/api/portal/procedure-approval-queue", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["appointments"]}
    assert requested.id in ids

    db.set_procedure_status(hospital_id, requested.id, "APPROVED")
    resp2 = client.get("/api/portal/procedure-approval-queue", headers=_auth(token))
    assert requested.id not in {a["id"] for a in resp2.json()["appointments"]}


def test_bookings_category_daycare_scopes_to_procedure_appointments_only(hospital_id):
    """The portal's own Daycare appointments page (category=daycare) is a
    full split out of category=diagnostic (its own separate sidebar
    section) -- confirms _apply_category_filter's new branch actually scopes
    GET /api/portal/bookings to just these rows, and that neither the
    "doctor" nor the "diagnostic" category (the Diagnostic & lab page) picks
    it up too."""
    procedure = _create_approval_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")
    requested = db.create_procedure_request(hospital_id, PHONE, procedure["id"], patient_name="Test", patient_date_of_birth=30)
    token = _login("adminpass")

    resp = client.get("/api/portal/bookings?category=daycare", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["appointments"][0]["id"] == requested.id

    resp2 = client.get("/api/portal/bookings?category=doctor", headers=_auth(token))
    assert resp2.status_code == 200, resp2.text
    assert requested.id not in {a["id"] for a in resp2.json()["appointments"]}

    resp3 = client.get("/api/portal/bookings?category=diagnostic", headers=_auth(token))
    assert resp3.status_code == 200, resp3.text
    assert requested.id not in {a["id"] for a in resp3.json()["appointments"]}


def test_new_daycare_booking_instant_procedure_books_a_real_slot(hospital_id):
    """Portal-side sibling of the WhatsApp instant-booking path -- staff can
    book a walk-in daycare/procedure appointment straight away (no approval
    step) via POST /api/portal/new-daycare-booking, same as
    portal_create_new_test_booking already does for diagnostic/lab."""
    procedure = _create_instant_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")
    token = _login("adminpass")

    slots_resp = client.get(
        f"/api/portal/new-booking/slots?procedure_id={procedure['id']}", headers=_auth(token),
    )
    assert slots_resp.status_code == 200, slots_resp.text
    slots_by_date = slots_resp.json()["slots_by_date"]
    first_slot_id = next(iter(slots_by_date.values()))[0]["id"]

    resp = client.post(
        "/api/portal/new-daycare-booking",
        headers=_auth(token),
        json={"patient_name": "Walk-in Patient", "patient_phone": "5490009999", "procedure_id": procedure["id"], "slot_id": first_slot_id},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["procedure_status"] == "CONFIRMED"

    created = next(a for a in db.get_all_appointments_for_hospital(hospital_id) if a.phone == "5490009999")
    assert created.procedure_id == procedure["id"]
    assert created.scheduled_at.isoformat() == first_slot_id


def test_new_daycare_booking_approval_required_procedure_creates_a_request(hospital_id):
    """The approval-required path takes no slot at all -- it lands in the
    Daycare page's own approval queue (procedure_status REQUESTED) instead
    of being immediately scheduled."""
    procedure = _create_approval_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")
    token = _login("adminpass")

    resp = client.post(
        "/api/portal/new-daycare-booking",
        headers=_auth(token),
        json={"patient_name": "Request Patient", "patient_phone": "5490008888", "procedure_id": procedure["id"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["procedure_status"] == "REQUESTED"

    queue = client.get("/api/portal/procedure-approval-queue", headers=_auth(token)).json()["appointments"]
    assert any(a["phone"] == "5490008888" for a in queue)


def test_new_daycare_booking_rejects_missing_slot_for_instant_procedure(hospital_id):
    procedure = _create_instant_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")
    token = _login("adminpass")

    resp = client.post(
        "/api/portal/new-daycare-booking",
        headers=_auth(token),
        json={"patient_name": "Test", "patient_phone": "5490007777", "procedure_id": procedure["id"]},
    )
    assert resp.status_code == 400, resp.text
    assert "slot" in resp.json()["errors"][0].lower()


# --- Request Reschedule (approval-required procedures only) ---

@pytest.mark.asyncio
async def test_request_reschedule_does_not_move_the_appointment_until_approved(hospital_id, sessions):
    from flows.booking.reschedule import _start_reschedule_flow_for_appointment

    procedure = _create_approval_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")
    slots = db.get_procedure_available_slots(hospital_id, procedure["id"])
    original_scheduled_at = datetime.fromisoformat(slots[0]["id"])
    appt = db.create_procedure_appointment(
        hospital_id, PHONE, procedure["id"], original_scheduled_at, patient_name="Test", patient_date_of_birth=30,
    )

    wa = FakeWhatsAppClient()
    connector = Tier1Connector()
    # Entered directly (mirrors the real entry point: a "manage_reschedule_
    # <id>" quick action, intercepted by flows/router.py before normal
    # session dispatch -- flows.booking.dispatch's own handle_incoming,
    # exercised by every other test in this file, doesn't wire that
    # interception itself) -- exercises this module's own mini-flow
    # (_handle_awaiting_procedure_reschedule_date/slot) from there on via
    # the normal dispatcher.
    await _start_reschedule_flow_for_appointment(wa, sessions, PHONE, hospital_id, appt, connector, language="en")
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_PROCEDURE_RESCHEDULE_DATE"

    new_slots = db.get_procedure_available_slots(hospital_id, procedure["id"])
    other_slot = next(s for s in new_slots if s["id"] != slots[0]["id"])
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(other_slot["date"]))
    matching = next(s for s in new_slots if s["date"] == other_slot["date"] and s["id"] != slots[0]["id"])
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap(matching["id"]))

    unchanged = db.get_appointment(hospital_id, appt.id)
    assert unchanged.scheduled_at == original_scheduled_at  # not moved yet
    assert unchanged.procedure_reschedule_requested_at is not None

    token = _login("adminpass")
    resp = client.post(f"/api/portal/bookings/{appt.id}/procedure/reschedule-request/approve", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    moved = db.get_appointment(hospital_id, appt.id)
    assert moved.scheduled_at.isoformat() == matching["id"]
    assert moved.procedure_reschedule_requested_at is None


def test_reject_reschedule_request_leaves_appointment_untouched(hospital_id):
    procedure = _create_approval_procedure(hospital_id)
    _set_hospital_password(hospital_id, "adminpass")
    slots = db.get_procedure_available_slots(hospital_id, procedure["id"])
    scheduled_at = datetime.fromisoformat(slots[0]["id"])
    appt = db.create_procedure_appointment(
        hospital_id, PHONE, procedure["id"], scheduled_at, patient_name="Test", patient_date_of_birth=30,
    )
    db.request_procedure_reschedule(hospital_id, appt.id, scheduled_at + timedelta(days=1))

    token = _login("adminpass")
    resp = client.post(f"/api/portal/bookings/{appt.id}/procedure/reschedule-request/reject", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    unchanged = db.get_appointment(hospital_id, appt.id)
    assert unchanged.scheduled_at == scheduled_at
    assert unchanged.procedure_reschedule_requested_at is None
