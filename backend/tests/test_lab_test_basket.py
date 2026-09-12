# tests/test_lab_test_basket.py
"""Lab Test Phase 2 follow-up (business spec Sections 4.1-4.4): the
multi-test basket -> collection method (visit vs. serviceability-gated home
sample collection) -> date/time -> confirm flow, its itemized price review,
reschedule carry-forward, and the document-upload-triggered report-ready
notification. Diagnostic Test's own single-item flow
(tests/test_diagnostic_test_scheduling.py) is untouched by any of this -- see that
file's own tests, which must stay green alongside these."""
import os
from datetime import timedelta

import pytest

import db.repository as db
from connectors import Tier1Connector
from core.session_store import InMemorySessionStore
from core.whatsapp import WhatsAppClient
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

PHONE = "+15550001111"


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


def _last(wa, kind: str):
    for k, kwargs in reversed(wa.sent):
        if k == kind:
            return kwargs
    return None


@pytest.fixture
def sessions():
    return InMemorySessionStore()


def _lab_tests(hospital_id):
    return db.get_diagnostic_tests(hospital_id, "lab")


def _configure_all_lab_tests_schedule(hospital_id):
    """Confirmed with the user directly: a test with no schedule configured
    no longer falls back to any-doctor-with-open-slots -- it's simply "not
    available" until an admin configures one (same as an unconfigured
    doctor). Every test below that drives a basket through to date/time
    needs a real schedule first; only the basket's first-added item's own
    schedule actually anchors the offered slots (flows/booking/types/lab.py's
    _basket_anchor), but which item goes first varies per test below, so
    every lab test gets the same schedule here."""
    for test in _lab_tests(hospital_id):
        db.update_diagnostic_test(
            hospital_id, test["id"], test["name"],
            working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], working_hours=["09:00-17:00"], slot_duration_minutes=30,
        )


def _set_test_price(hospital_id, test: dict, price: float) -> None:
    """Test/variant merge: price lives directly on the test row now, so
    setting it means re-passing its already-configured schedule too (a bare
    price-only update() would otherwise wipe working_days/hours back to
    blank defaults)."""
    full = db.get_diagnostic_test_full(hospital_id, test["id"])
    db.update_diagnostic_test(
        hospital_id, test["id"], test["name"], price=price,
        working_days=full["working_days"], working_hours=full["working_hours"],
        slot_duration_minutes=full["slot_duration_minutes"], breaks=full["breaks"],
        max_bookings_per_slot=full["max_bookings_per_slot"], daily_booking_limit=full["daily_booking_limit"],
        effective_from=full["effective_from"],
    )


async def _start_lab_booking(wa, sessions, hospital_id, phone: str = PHONE):
    sessions.set(hospital_id, phone, "AWAITING_APPOINTMENT_TYPE", {"patient_name": "Priya Singh", "patient_date_of_birth": 29})
    await handle_incoming(wa, sessions, phone, hospital_id, tap("lab"))


async def _add_test_to_basket(wa, sessions, hospital_id, test_name: str, phone: str = PHONE):
    """Taps the named test directly from the (already-shown) test list --
    every call after the first one in a basket picks straight from the same
    remaining-tests list re-shown after the previous add (WhatsApp menu
    restructuring follow-up: no more "Add Another Test" detour screen),
    landing back at AWAITING_LAB_TEST with the list re-shown."""
    test = next(t for t in _lab_tests(hospital_id) if t["name"] == test_name)
    await handle_incoming(wa, sessions, phone, hospital_id, tap(str(test["id"])))
    assert sessions.get(hospital_id, phone)["state"] == "AWAITING_LAB_TEST"


async def _finish_basket_and_pick_visit(wa, sessions, hospital_id, phone: str = PHONE):
    await handle_incoming(wa, sessions, phone, hospital_id, tap("lab_done"))
    assert sessions.get(hospital_id, phone)["state"] == "AWAITING_COLLECTION_METHOD"
    await handle_incoming(wa, sessions, phone, hospital_id, tap("collection_visit"))


async def _drive_to_confirmation(wa, sessions, hospital_id, phone: str = PHONE):
    context = sessions.get(hospital_id, phone)["context"]
    resource_id = context.get("resource_id")
    doctor_id = context.get("doctor_id")
    slots = db.get_test_slots(hospital_id, resource_id) if resource_id else db.get_slots(hospital_id, doctor_id)
    date_str = slots[0]["date"]
    await handle_incoming(wa, sessions, phone, hospital_id, tap(date_str))
    slot = next(s for s in slots if s["date"] == date_str)
    await handle_incoming(wa, sessions, phone, hospital_id, tap(slot["id"]))
    assert sessions.get(hospital_id, phone)["state"] == "AWAITING_CONFIRMATION"


# --- Multi-test basket, visit collection ---

@pytest.mark.asyncio
async def test_multi_test_basket_books_all_selected_tests(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    _configure_all_lab_tests_schedule(hospital_id)
    tests = _lab_tests(hospital_id)
    _set_test_price(hospital_id, tests[0], 500)
    _set_test_price(hospital_id, tests[1], 800)

    await _start_lab_booking(wa, sessions, hospital_id)
    await _add_test_to_basket(wa, sessions, hospital_id, tests[0]["name"])
    await _add_test_to_basket(wa, sessions, hospital_id, tests[1]["name"])
    await _finish_basket_and_pick_visit(wa, sessions, hospital_id)
    await _drive_to_confirmation(wa, sessions, hospital_id)

    kind, kwargs = wa.sent[-1]
    assert kind == "buttons"
    assert tests[0]["name"] in kwargs["body_text"]
    assert tests[1]["name"] in kwargs["body_text"]
    assert "1,300" in kwargs["body_text"] or "1300" in kwargs["body_text"]

    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))
    appt = next(a for a in db.get_upcoming_appointments(hospital_id, offset_hours=999999) if a.phone == PHONE)
    assert appt.lab_status == "booked"
    assert appt.collection_method == "visit"
    basket = db.get_lab_basket_for_appointment(hospital_id, appt.id)
    assert {item["test_label"] for item in basket} == {tests[0]["name"], tests[1]["name"]}
    assert sorted(item["price"] for item in basket) == [500, 800]


@pytest.mark.asyncio
async def test_already_added_test_excluded_from_further_selection(hospital_id, sessions):
    """The remaining-tests list is re-shown immediately after adding a test
    (no separate "Add Another Test" tap needed) -- already-added tests are
    excluded from it."""
    wa = FakeWhatsAppClient()
    tests = _lab_tests(hospital_id)
    await _start_lab_booking(wa, sessions, hospital_id)
    await _add_test_to_basket(wa, sessions, hospital_id, tests[0]["name"])
    kwargs = _last(wa, "list")
    row_ids = {r["id"] for s in kwargs["sections"] for r in s["rows"]}
    assert str(tests[0]["id"]) not in row_ids


# --- Collection method: home sample collection, serviceability-gated ---

@pytest.mark.asyncio
async def test_home_collection_serviceable_pincode_asks_address_and_adds_charge(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    db.create_service_area(hospital_id, "110001")
    db.update_hospital_settings(hospital_id, followup_validity_days=None, followup_fee=None, new_consultation_fee=None, home_collection_charge=150)
    _configure_all_lab_tests_schedule(hospital_id)
    tests = _lab_tests(hospital_id)
    _set_test_price(hospital_id, tests[0], 500)

    await _start_lab_booking(wa, sessions, hospital_id)
    await _add_test_to_basket(wa, sessions, hospital_id, tests[0]["name"])
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("lab_done"))
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("collection_home"))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_COLLECTION_PINCODE"

    await handle_incoming(wa, sessions, PHONE, hospital_id, text("110001"))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_COLLECTION_ADDRESS"

    await handle_incoming(wa, sessions, PHONE, hospital_id, text("221B Baker Street, New Delhi"))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_DATE"

    await _drive_to_confirmation(wa, sessions, hospital_id)
    _, kwargs = wa.sent[-1]
    assert "221B Baker Street" in kwargs["body_text"]
    assert "150" in kwargs["body_text"]
    assert "650" in kwargs["body_text"]  # 500 test charge + 150 home collection

    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))
    appt = next(a for a in db.get_upcoming_appointments(hospital_id, offset_hours=999999) if a.phone == PHONE)
    assert appt.collection_method == "home"
    assert appt.collection_pincode == "110001"
    assert appt.collection_address == "221B Baker Street, New Delhi"
    assert appt.home_collection_charge == 150


@pytest.mark.asyncio
async def test_non_serviceable_pincode_offers_visit_fallback_instead_of_dead_end(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    _configure_all_lab_tests_schedule(hospital_id)
    tests = _lab_tests(hospital_id)

    await _start_lab_booking(wa, sessions, hospital_id)
    await _add_test_to_basket(wa, sessions, hospital_id, tests[0]["name"])
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("lab_done"))
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("collection_home"))

    await handle_incoming(wa, sessions, PHONE, hospital_id, text("999999"))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_COLLECTION_PINCODE"
    kind, kwargs = wa.sent[-1]
    assert kind == "buttons"
    ids = {b["id"] for b in kwargs["buttons"]}
    assert "collection_visit" in ids

    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("collection_visit"))
    assert sessions.get(hospital_id, PHONE)["state"] == "AWAITING_DATE"
    assert sessions.get(hospital_id, PHONE)["context"]["collection_method"] == "visit"


# --- Reschedule carries the basket + collection details forward ---

@pytest.mark.asyncio
async def test_reschedule_lab_booking_carries_basket_and_collection_forward(hospital_id, sessions):
    wa = FakeWhatsAppClient()
    db.create_service_area(hospital_id, "110001")
    _configure_all_lab_tests_schedule(hospital_id)
    tests = _lab_tests(hospital_id)
    _set_test_price(hospital_id, tests[0], 400)
    _set_test_price(hospital_id, tests[1], 600)

    await _start_lab_booking(wa, sessions, hospital_id)
    await _add_test_to_basket(wa, sessions, hospital_id, tests[0]["name"])
    await _add_test_to_basket(wa, sessions, hospital_id, tests[1]["name"])
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("lab_done"))
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("collection_home"))
    await handle_incoming(wa, sessions, PHONE, hospital_id, text("110001"))
    await handle_incoming(wa, sessions, PHONE, hospital_id, text("42 MG Road"))
    await _drive_to_confirmation(wa, sessions, hospital_id)
    await handle_incoming(wa, sessions, PHONE, hospital_id, tap("confirm"))

    original = next(a for a in db.get_upcoming_appointments(hospital_id, offset_hours=999999) if a.phone == PHONE)
    original_basket = db.get_lab_basket_for_appointment(hospital_id, original.id)

    connector = Tier1Connector()
    new_appointment = connector.reschedule_booking(
        hospital_id, original.id, PHONE, original.department_id, original.doctor_id,
        original.scheduled_at + timedelta(minutes=30), diagnostic_test_id=original.diagnostic_test_id,
    )
    assert new_appointment.collection_method == "home"
    assert new_appointment.collection_address == "42 MG Road"
    assert new_appointment.collection_pincode == "110001"
    assert new_appointment.lab_status == "booked"
    new_basket = db.get_lab_basket_for_appointment(hospital_id, new_appointment.id)
    assert {item["test_label"] for item in new_basket} == {item["test_label"] for item in original_basket}
    assert len(new_basket) == len(original_basket)


# --- Repo-level: serviceable-PIN-code list ---

def test_lab_service_area_crud_and_serviceability(hospital_id):
    assert db.is_pincode_serviceable(hospital_id, "560001") is False
    area = db.create_service_area(hospital_id, "560001")
    assert db.is_pincode_serviceable(hospital_id, "560001") is True
    db.set_service_area_active(hospital_id, area["id"], False)
    assert db.is_pincode_serviceable(hospital_id, "560001") is False
    db.set_service_area_active(hospital_id, area["id"], True)
    assert db.is_pincode_serviceable(hospital_id, "560001") is True
    assert db.delete_service_area(hospital_id, area["id"]) is True
    assert db.is_pincode_serviceable(hospital_id, "560001") is False


def test_lab_service_area_range_matches_inside_and_rejects_outside(hospital_id):
    area = db.create_service_area(hospital_id, range_start="400001", range_end="400050")
    assert area["pincode"] is None
    assert area["range_start"] == "400001" and area["range_end"] == "400050"
    assert db.is_pincode_serviceable(hospital_id, "400001") is True
    assert db.is_pincode_serviceable(hospital_id, "400025") is True
    assert db.is_pincode_serviceable(hospital_id, "400050") is True
    assert db.is_pincode_serviceable(hospital_id, "400051") is False
    assert db.is_pincode_serviceable(hospital_id, "399999") is False
    db.set_service_area_active(hospital_id, area["id"], False)
    assert db.is_pincode_serviceable(hospital_id, "400025") is False


def test_lab_service_area_range_validation_rejects_bad_input(hospital_id):
    with pytest.raises(db.InvalidPincodeRange):
        db.create_service_area(hospital_id, range_start="400050", range_end="400001")
    with pytest.raises(db.InvalidPincodeRange):
        db.create_service_area(hospital_id, range_start="abcdef", range_end="400050")
    with pytest.raises(db.InvalidPincodeRange):
        db.create_service_area(hospital_id, range_start="4001", range_end="4050")
    with pytest.raises(db.InvalidPincodeRange):
        db.create_service_area(hospital_id)


def test_portal_lab_service_area_range_create_and_reject(hospital_id):
    _set_hospital_creds(hospital_id, password="testpass123", phone_number_id="pn1", access_token="tok1")
    token = _login("testpass123")

    resp = client.post(
        "/api/portal/lab-service-areas", headers=_auth(token),
        json={"range_start": "400001", "range_end": "400050"},
    )
    assert resp.status_code == 200, resp.text
    area = resp.json()["lab_service_area"]
    assert area["range_start"] == "400001" and area["range_end"] == "400050"

    resp = client.post(
        "/api/portal/lab-service-areas", headers=_auth(token),
        json={"range_start": "400050", "range_end": "400001"},
    )
    assert resp.status_code == 400

    resp = client.post("/api/portal/lab-service-areas", headers=_auth(token), json={})
    assert resp.status_code == 400


# --- Report lifecycle: staff advance + document-upload-triggered report_ready ---

def _set_hospital_creds(hosp_id: int, *, password: str, phone_number_id: str, access_token: str) -> None:
    h = db.get_hospital(hosp_id)
    db.update_hospital(
        hosp_id, name=h.name, whatsapp_phone_number_id=phone_number_id, access_token=access_token,
        app_secret=h.app_secret, timezone=h.timezone, welcome_message_text=h.welcome_message_text,
        reminder_offsets_hours=h.reminder_offsets_hours, reminder_template_name=h.reminder_template_name,
        data_tier=h.data_tier, external_api_base_url=h.external_api_base_url,
        external_api_key=h.external_api_key, portal_password_hash=db.hash_portal_password(password),
        enabled_features=h.enabled_features,
    )


def _login(password: str) -> str:
    resp = client.post("/api/portal/login", json={"password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_lab_appointment(hosp_id: int, phone: str) -> int:
    from datetime import datetime
    department = db.get_departments(hosp_id)[0]
    doctors = db.get_doctors(hosp_id, department["id"])
    scheduled_at = datetime.fromisoformat(db.get_slots(hosp_id, doctors[0]["id"])[0]["id"])
    appt = db.create_appointment(hosp_id, phone, department["id"], doctors[0]["id"], scheduled_at)
    db.set_appointment_lab_order_details(
        hosp_id, appt.id, "visit", None, None, None,
        [{"diagnostic_test_id": None, "test_label": "CBC", "price": 300}],
    )
    return appt.id


def test_lab_status_advances_forward_only_never_directly_to_report_ready(hospital_id):
    _set_hospital_creds(hospital_id, password="testpass123", phone_number_id="pn1", access_token="tok1")
    token = _login("testpass123")
    appt_id = _create_lab_appointment(hospital_id, "+15551230000")

    resp = client.post(f"/api/portal/bookings/{appt_id}/lab-status", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["appointment"]["lab_status"] == "sample_collected"

    resp = client.post(f"/api/portal/bookings/{appt_id}/lab-status", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["appointment"]["lab_status"] == "processing"

    # Staff can never advance straight to report_ready -- only the
    # document-upload trigger below can do that.
    resp = client.post(f"/api/portal/bookings/{appt_id}/lab-status", headers=_auth(token))
    assert resp.status_code == 400


def test_bookings_lab_status_filter_is_independent_of_booking_status(hospital_id):
    """The portal's Diagnostic & lab appointments page now filters Booking
    Status and Lab Status separately (two columns, two FilterSelects) --
    confirms GET /api/portal/bookings?lab_status= scopes correctly and
    doesn't require `status` to also be set."""
    _set_hospital_creds(hospital_id, password="labstatusfilter-pw", phone_number_id="pn3", access_token="tok3")
    token = _login("labstatusfilter-pw")

    appt_id = _create_lab_appointment(hospital_id, "+15551239999")
    other_appt_id = _create_lab_appointment(hospital_id, "+15551238888")
    resp = client.post(f"/api/portal/bookings/{appt_id}/lab-status", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["appointment"]["lab_status"] == "sample_collected"

    resp = client.get("/api/portal/bookings?lab_status=sample_collected", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["appointments"]}
    assert appt_id in ids
    assert other_appt_id not in ids

    resp = client.get("/api/portal/bookings?lab_status=booked", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["appointments"]}
    assert other_appt_id in ids
    assert appt_id not in ids


def test_diagnostic_status_advances_straight_to_processing_skipping_sample_collection(hospital_id):
    """Diagnostics/imaging (MRI, CT Scan, ...) shares the exact same
    lab_status lifecycle/endpoint as Lab Test -- it just skips the physical
    "Sample Collected" stage (nothing to collect for a scan), going straight
    booked -> processing, same as the Lab Test test above but one step
    shorter."""
    from datetime import datetime

    _set_hospital_creds(hospital_id, password="testpass456", phone_number_id="pn2", access_token="tok2")
    token = _login("testpass456")

    test = db.create_diagnostic_test(
        hospital_id, "diagnostic", "MRI Scan",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        working_hours=["09:00-13:00"], slot_duration_minutes=30,
    )
    slot = db.get_test_slots(hospital_id, test["id"])[0]
    scheduled_at = datetime.fromisoformat(slot["id"])
    appt = db.create_appointment(
        hospital_id, "+15551231111", None, None, scheduled_at,
        appointment_type_id="diagnostic", diagnostic_test_id=test["id"],
        diagnostic_test_label="MRI Scan", lab_status="booked",
    )

    resp = client.post(f"/api/portal/bookings/{appt.id}/lab-status", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["appointment"]["lab_status"] == "processing"

    # No further manual advance -- report_ready is upload-triggered only,
    # same as Lab Test.
    resp = client.post(f"/api/portal/bookings/{appt.id}/lab-status", headers=_auth(token))
    assert resp.status_code == 400


def test_uploading_lab_report_sets_report_ready_and_notifies_patient(hospital_id, monkeypatch):
    _set_hospital_creds(hospital_id, password="testpass123", phone_number_id="pn1", access_token="tok1")
    token = _login("testpass123")
    phone = "+15559998765"
    appt_id = _create_lab_appointment(hospital_id, phone)
    patient = db.get_patient_by_phone(hospital_id, phone)

    sent = []

    async def fake_send_text(self, to, message):
        sent.append((to, message))
        return True

    monkeypatch.setattr(WhatsAppClient, "send_text", fake_send_text)

    resp = client.post(
        f"/api/portal/patients/{patient['id']}/documents",
        headers=_auth(token),
        files={"file": ("report.pdf", b"%PDF-1.4 fake report", "application/pdf")},
        data={"document_type": "lab_report", "appointment_id": str(appt_id)},
    )
    assert resp.status_code == 200, resp.text

    appt = db.get_appointment(hospital_id, appt_id)
    assert appt.lab_status == "report_ready"
    assert len(sent) == 1
    assert sent[0][0] == phone
