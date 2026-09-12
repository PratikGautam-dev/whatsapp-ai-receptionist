import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { portalFetch } from "@/lib/portalAuth";
import { toast } from "@/lib/toast";

const PAGE_SIZE = 10;

const DEFAULT_CANCEL_MESSAGE = "Your appointment has been cancelled.";
const DEFAULT_RESCHEDULE_MESSAGE = "Your appointment has been rescheduled.";

export type Appointment = {
  id: number;
  phone: string;
  patient_name: string | null;
  department_id: string | null;
  department_name: string | null;
  doctor_id: string | null;
  doctor_name: string | null;
  // Diagnostic/Lab reschedule follow-up: set (doctor_id/doctor_name both
  // null) for a resource-bound booking -- an MRI machine or lab collection
  // point, not a doctor. Diagnostic tests/resources merge: this is a
  // diagnostic_tests.id now -- a test IS the schedulable resource.
  diagnostic_test_id: number | null;
  diagnostic_test_name: string | null;
  scheduled_at: string;
  status: string;
  source: string;
  reference_id: string | null;
  patient_display_id: string | null;
  appointment_type_id: string | null;
  video_link: string | null;
  created_at: string | null;
  // Lab Test Phase 2 follow-up: null for every non-Lab-Test appointment.
  lab_status: string | null;
  // Daycare/Procedure rebuild: null for every non-procedure appointment.
  // scheduled_at above is a PLACEHOLDER (request creation time) until
  // procedure_status reaches "CONFIRMED" -- don't display it as a real
  // slot before then.
  procedure_id: number | null;
  procedure_name: string | null;
  procedure_status: string | null;
  procedure_estimated_price_min: number | null;
  procedure_estimated_price_max: number | null;
  procedure_order_reference: string | null;
  procedure_reschedule_requested_at: string | null;
  // Daycare/Procedure rebuild: which concrete bed/chair/equipment/staff this
  // booking is bound to -- empty for every non-procedure appointment (and
  // for one not yet CONFIRMED, since resources are only reserved at that
  // point).
  procedure_resources: { id: number; resource_id: number; resource_type: string; resource_name: string }[];
};

export type Department = { id: string; name: string };
export type Doctor = { id: string; name: string };
// category/price (added for the portal's multi-test lab booking basket --
// NewTestBookingDialog groups by category and shows a price) are absent
// from the plain reschedule-context caller's own reads, which is fine --
// TypeScript structural typing doesn't require every consumer to use every
// field.
export type Resource = { id: number; name: string; category: "diagnostic" | "lab"; price: number | null };
export type Slot = { id: string; label: string };
export type SlotsByDate = Record<string, Slot[]>;
export type NewBookingContext = {
  departments: Department[];
  doctors_by_department: Record<string, Doctor[]>;
  // Diagnostic/Lab reschedule follow-up: the resource-bound equivalent of
  // doctors_by_department above -- a resource has no department picker of
  // its own in this dialog, so just a flat list. Slots for a specific
  // doctor/resource are fetched separately, on demand, via
  // GET /api/portal/new-booking/slots?doctor_id=/?diagnostic_test_id= (see
  // fetchSlotsByDate below) -- not eager-loaded here for every doctor/
  // resource, which is what this endpoint used to do before it became slow
  // enough to notice.
  resources: Resource[];
};

/** Fetches available slots for exactly ONE doctor, ONE resource, or ONE
 * (instant-booking) procedure (pass exactly one), grouped by date -- the
 * lazy counterpart to the context endpoint above. Shared by every consumer
 * that used to read ctx.slots_by_doctor[id]/slots_by_resource[id] out of
 * the old eager all-at-once context: NewBookingDialog/NewTestBookingDialog/
 * NewDaycareBookingDialog (via their own hooks), RescheduleDialog (below),
 * and the patient page's follow-up "Book now" panel (usePatientDetail.ts). */
export async function fetchSlotsByDate(
  router: ReturnType<typeof useRouter>, opts: { doctorId?: string; resourceId?: string; procedureId?: string },
): Promise<SlotsByDate | null> {
  const params = new URLSearchParams();
  if (opts.doctorId) params.set("doctor_id", opts.doctorId);
  if (opts.resourceId) params.set("diagnostic_test_id", opts.resourceId);
  if (opts.procedureId) params.set("procedure_id", opts.procedureId);
  const result = await portalFetch(`/api/portal/new-booking/slots?${params.toString()}`);
  if (!result.ok) {
    if (result.unauthorized) router.push("/portal/login");
    return null;
  }
  return (result.data as { slots_by_date: SlotsByDate }).slots_by_date;
}

// docs/per-appointment-type-flow-plan.md's fixed catalog (db/repositories/
// appointment_types.py's DEFAULT_APPOINTMENT_TYPES) -- there's no portal CRUD
// for appointment types (seeded once, at onboarding), so this mirrors that
// same fixed id->label mapping rather than fetching it from a new endpoint.
export const TYPE_LABELS: Record<string, string> = {
  new: "New Consultation",
  followup: "Follow-up",
  tele: "Tele-consultation",
  second_opinion: "Second Opinion",
  diagnostic: "Diagnostic",
  lab: "Lab Test",
  daycare: "Daycare",
};

// Mirrors backend/db/repositories/appointments.py's own
// _apply_category_filter -- "doctor"/"diagnostic" is the same 2-way split
// the WhatsApp booking menu and the portal sidebar (Doctor appointments /
// Diagnostic & lab / Report review) use; "daycare" is its own further split
// out of what used to be lumped into "diagnostic" (Daycare/Procedure
// rebuild), since it now has its own sidebar section. Scoping by category
// happens server-side (get_appointments_page's own `category` param) --
// this type just labels which scope a page's hook call wants.
export type AppointmentCategory = "all" | "doctor" | "diagnostic" | "daycare";

// Shared vocabulary for every list page's tab pills (appointments/page.tsx's
// Today/Upcoming/Completed/Cancelled, diagnostic/page.tsx's own
// Diagnostics/Lab tests/Completed/Pending/Cancelled) -- each page still owns
// its own tab id/label list (they differ per page), it just needs to be
// drawn from this set so tabToServerParams below knows every id. Resolving
// a tab to server params (rather than filtering the loaded page client-side,
// like both pages used to) is what keeps a tab pill meaning "every matching
// row", not just whichever ones landed on the current 10-row page.
export type AppointmentTab =
  | "all" | "today" | "upcoming" | "previous"
  | "completed" | "cancelled" | "pending" | "diagnostics" | "lab";

function tabToServerParams(tab: string): { status?: string; type?: string; when?: string } {
  switch (tab as AppointmentTab) {
    case "today": return { when: "today" };
    case "upcoming": return { when: "upcoming" };
    // Same "attended, regardless of date" meaning as "completed" below --
    // "previous" is the Doctor/Diagnostic & lab pages' shared All/Today/
    // Upcoming/Previous tab set replacing the old, more granular per-page
    // tab lists (completed/cancelled/pending/diagnostics/lab, still kept
    // here for whichever page hasn't moved onto that shared set yet).
    case "previous": return { status: "attended" };
    case "completed": return { status: "attended" };
    case "cancelled": return { status: "cancelled" };
    case "pending": return { status: "booked" };
    case "diagnostics": return { type: "diagnostic" };
    case "lab": return { type: "lab" };
    default: return {};
  }
}

/** Combines modeFilter (Doctor appointments page's own "all"/"walk_in"/
 * "tele" split) with typeFilter (new/followup/tele) into the single `type`
 * query value get_appointments_page() understands -- a bare value for an
 * exact match, or a comma-separated pair for "walk_in"'s "not tele" (which
 * has no single appointment_type_id of its own). typeFilter, when it isn't
 * "all", already narrows to one specific type and takes precedence -- mode
 * only fills in the gap typeFilter left at "all". */
function resolveTypeParam(mode: string, type: string): string {
  if (type !== "all") return type;
  if (mode === "walk_in") return "new,followup";
  if (mode === "tele") return "tele";
  return "";
}

/** Loads + owns every mutation on the /portal/appointments list -- cancel,
 * reschedule, attendance marking, delete -- plus the search/status/type
 * filters, pagination, and the cancel/reschedule inline panels' own form
 * state. `category` scopes the loaded list itself (not just a tab filter)
 * -- "doctor" for the Doctor appointments page, "diagnostic" for the
 * Diagnostic & lab test page, "all" (default) elsewhere. `tab` is the
 * calling page's own current tab pill id (see AppointmentTab) -- passed in
 * rather than owned here since each page's tab labels/ids differ; this
 * hook only needs the id to resolve it to server params. */
export function useAppointments(ready: boolean, category: AppointmentCategory = "all", tab: string = "all") {
  const router = useRouter();
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  const [cancelPanelId, setCancelPanelId] = useState<number | null>(null);
  const [cancelMessage, setCancelMessage] = useState(DEFAULT_CANCEL_MESSAGE);

  const [reschedulePanelId, setReschedulePanelId] = useState<number | null>(null);
  const [reschedulingId, setReschedulingId] = useState<number | null>(null);
  const [rescheduleSlotsByDate, setRescheduleSlotsByDate] = useState<SlotsByDate | null>(null);
  const [rescheduleErrors, setRescheduleErrors] = useState<string[]>([]);
  const [rescheduleMessage, setRescheduleMessage] = useState(DEFAULT_RESCHEDULE_MESSAGE);
  // Department/doctor (or resource) are fixed to whichever the appointment
  // already has -- rescheduling only moves the date/slot, so this is set
  // once (from the appointment being rescheduled) when the dialog opens,
  // never from a user pick. Kept internal (not returned below) since
  // RescheduleDialog reads department_name/doctor_name/diagnostic_test_name
  // straight off the Appointment it's given for display -- these are only
  // here to fetch that one doctor's/resource's slots (rescheduleSlotsByDate).
  const [rDoctorId, setRDoctorId] = useState("");
  const [rResourceId, setRResourceId] = useState("");
  const [rDate, setRDate] = useState("");
  const [rSlotId, setRSlotId] = useState("");
  const [markingAttendanceId, setMarkingAttendanceId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [pendingDelete, setPendingDelete] = useState<Appointment[] | null>(null);
  const [bulkDeleting, setBulkDeleting] = useState(false);

  const [page, setPage] = useState(1);

  // "Draft" values -- what the search box/FilterSelect dropdowns show and
  // update live as the user types/picks. Nothing is sent to the server
  // until applyFilters() runs (the Filter button); resetFilters() clears
  // both draft and applied. Same staged-then-Apply pattern sarvaya-
  // dashboard's own filter bars (ExpenseFilterBar etc.) use -- a network
  // request per keystroke isn't wanted now that search hits the backend.
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  // Diagnostic & lab appointments page only -- independent of statusFilter
  // above (Booking Status vs. Lab Status are now two separate columns/
  // filters, not one merged "Status" cell). Harmless no-op for every other
  // page's hook call, which just never sets it away from "all".
  const [labStatusFilter, setLabStatusFilter] = useState("all");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [appliedStatus, setAppliedStatus] = useState("all");
  const [appliedType, setAppliedType] = useState("all");
  const [appliedLabStatus, setAppliedLabStatus] = useState("all");

  // "all" | "walk_in" | "tele" -- Doctor appointments page's own coarser
  // split on top of typeFilter's new/followup/tele. "walk_in" means
  // new+followup together (there's no single appointment_type_id for
  // "not tele"); "tele" is just the "tele" type again, but as its own mode
  // rather than a Type-dropdown pick, so the two combine to one server
  // query instead of the frontend trying to run two competing type filters.
  //
  // Unlike search/status/type above, this isn't staged behind Apply -- it
  // lives in the page header as its own action (not the filter toolbar), so
  // picking it refetches immediately. One state, not a draft+applied pair.
  const [modeFilter, setModeFilterState] = useState("all");
  const setModeFilter = useCallback((next: string) => {
    setModeFilterState(next);
    // A "tele" type pick only makes sense for mode "all" -- both the
    // Type dropdown's own options and any already-applied type value
    // must drop it once a mode is chosen, or the two would disagree
    // about whether tele rows should show.
    if (next !== "all") {
      setTypeFilter((current) => (current === "tele" ? "all" : current));
      setAppliedType((current) => (current === "tele" ? "all" : current));
    }
    setPage(1);
  }, []);

  // `overrides` lets a caller (e.g. a quick-action shortcut like "View
  // pending reports") apply a specific status/type immediately instead of
  // relying on the user having already picked it in the FilterSelect draft
  // first -- setting the draft state and calling applyFilters() right after
  // wouldn't work, since this closure still sees the OLD draft value until
  // the next render. Passing it here updates draft + applied together in
  // one go.
  const applyFilters = useCallback((overrides?: { search?: string; status?: string; type?: string; labStatus?: string }) => {
    const nextSearch = overrides?.search ?? searchQuery;
    const nextStatus = overrides?.status ?? statusFilter;
    const nextType = overrides?.type ?? typeFilter;
    const nextLabStatus = overrides?.labStatus ?? labStatusFilter;
    if (overrides?.search !== undefined) setSearchQuery(nextSearch);
    if (overrides?.status !== undefined) setStatusFilter(nextStatus);
    if (overrides?.type !== undefined) setTypeFilter(nextType);
    if (overrides?.labStatus !== undefined) setLabStatusFilter(nextLabStatus);
    setAppliedSearch(nextSearch.trim());
    setAppliedStatus(nextStatus);
    setAppliedType(nextType);
    setAppliedLabStatus(nextLabStatus);
    setPage(1);
  }, [searchQuery, statusFilter, typeFilter, labStatusFilter]);

  const resetFilters = useCallback(() => {
    setSearchQuery("");
    setStatusFilter("all");
    setTypeFilter("all");
    setLabStatusFilter("all");
    setModeFilterState("all");
    setAppliedSearch("");
    setAppliedStatus("all");
    setAppliedType("all");
    setAppliedLabStatus("all");
    setPage(1);
  }, []);

  const filtersDirty = searchQuery.trim() !== appliedSearch || statusFilter !== appliedStatus || typeFilter !== appliedType ||
    labStatusFilter !== appliedLabStatus;

  // A tab pill switch changes which rows exist at all -- staying on page 3
  // of the old tab would otherwise render an empty table under the new one.
  // Reset during render (React's own "adjusting state when a prop changes"
  // pattern) rather than in an effect, which would cause an extra render.
  const [prevTab, setPrevTab] = useState(tab);
  if (tab !== prevTab) {
    setPrevTab(tab);
    setPage(1);
  }

  const queryKey = [
    "portal-bookings", category, page, appliedSearch, appliedStatus, appliedType, appliedLabStatus, modeFilter, tab,
  ] as const;

  const {
    data, isFetching, error: queryError, refetch,
  } = useQuery({
    queryKey,
    enabled: ready,
    placeholderData: keepPreviousData,
    retry: false,
    queryFn: async () => {
      const params = new URLSearchParams({ page: String(page), limit: String(PAGE_SIZE) });
      if (category !== "all") params.set("category", category);
      const tabParams = tabToServerParams(tab);
      const status = tabParams.status || (appliedStatus !== "all" ? appliedStatus : "");
      const type = tabParams.type || resolveTypeParam(modeFilter, appliedType);
      if (status) params.set("status", status);
      if (type) params.set("type", type);
      if (appliedLabStatus !== "all") params.set("lab_status", appliedLabStatus);
      if (tabParams.when) params.set("when", tabParams.when);
      if (appliedSearch) params.set("search", appliedSearch);
      const result = await portalFetch(`/api/portal/bookings?${params.toString()}`);
      if (!result.ok) {
        if (result.unauthorized) router.push("/portal/login");
        throw new Error(result.unauthorized ? "Not authenticated." : result.error);
      }
      return result.data as { appointments: Appointment[]; total: number };
    },
  });

  const appointments = data?.appointments ?? null;
  const total = data?.total ?? 0;

  // Full (up to 500), category-scoped, unfiltered/unpaginated -- unlike
  // `appointments` above (this page's own 10-row slice of the table),
  // stat tiles/tab-count badges/"today's schedule"/lab-queue widgets all
  // need the WHOLE scoped dataset to compute their numbers from. Same
  // "small enough to fetch whole and compute client-side" reasoning this
  // hook always used, just no longer shared with the table's own fetch now
  // that the table itself needs to scale past 500 rows via real pagination.
  const { data: summaryData, refetch: refetchSummary } = useQuery({
    queryKey: ["portal-bookings-summary", category],
    enabled: ready,
    queryFn: async () => {
      const params = new URLSearchParams();
      if (category !== "all") params.set("category", category);
      const result = await portalFetch(`/api/portal/bookings/summary?${params.toString()}`);
      if (!result.ok) {
        if (result.unauthorized) router.push("/portal/login");
        throw new Error(result.unauthorized ? "Not authenticated." : result.error);
      }
      return (result.data as { appointments: Appointment[] }).appointments;
    },
  });
  const allAppointments = summaryData ?? null;

  const error = mutationError ?? (queryError ? "Couldn't load appointments — try again." : null);

  const load = useCallback(() => {
    refetch();
    refetchSummary();
  }, [refetch, refetchSummary]);

  // Shared success/error-toast handling for the many fire-and-forget row
  // actions below (attendance, lab status, procedure actions, delete) --
  // every one of them used to just no-op on failure with zero feedback.
  function afterAction(result: Awaited<ReturnType<typeof portalFetch>>, successMessage: string, failureMessage: string): boolean {
    if (result.ok) {
      toast.success(successMessage);
      return true;
    }
    if (result.unauthorized) {
      router.push("/portal/login");
      return false;
    }
    toast.error(failureMessage, result.error);
    return false;
  }

  // Item 9 (Spec.md Section 0): closes the "no-shows are a heuristic, not a
  // real status" gap -- a still-'booked' appointment whose scheduled time
  // has already passed gets an inline "Did the patient visit?" prompt,
  // computed from fields the list already has (no separate fetch needed).
  async function handleAttendance(id: number, attended: boolean) {
    setMarkingAttendanceId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/attendance`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ attended }),
    });
    setMarkingAttendanceId(null);
    if (afterAction(result, attended ? "Marked as attended" : "Marked as no-show", "Couldn't update attendance")) load();
  }

  // Lab Test Phase 2 follow-up: advances booked -> sample_collected ->
  // processing one step at a time -- report_ready is never set from here,
  // only automatically, by uploading a lab_report document against the
  // appointment (the Patients page's document upload, not this list).
  const [advancingLabStatusId, setAdvancingLabStatusId] = useState<number | null>(null);
  async function handleAdvanceLabStatus(id: number) {
    setAdvancingLabStatusId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/lab-status`, { method: "POST" });
    setAdvancingLabStatusId(null);
    if (afterAction(result, "Lab status updated", "Couldn't update lab status")) load();
  }

  // Daycare/Procedure rebuild: approve/reject a pending request, or advance
  // an already-CONFIRMED procedure's status (CONFIRMED -> COMPLETED, or ->
  // CANCELLED) -- same "one action in flight per row" shape as the lab-status
  // advance above.
  const [procedureActionId, setProcedureActionId] = useState<number | null>(null);
  async function handleApproveProcedureRequest(id: number) {
    setProcedureActionId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/procedure/approve`, { method: "POST" });
    setProcedureActionId(null);
    if (afterAction(result, "Request approved", "Couldn't approve request")) load();
  }
  async function handleRejectProcedureRequest(id: number, reason?: string) {
    setProcedureActionId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/procedure/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason || "" }),
    });
    setProcedureActionId(null);
    if (afterAction(result, "Request rejected", "Couldn't reject request")) load();
  }
  // `status` lets a caller force the explicit CANCELLED branch the backend
  // route accepts (valid from any non-terminal procedure_status) --
  // omitted, it just advances one step forward (CONFIRMED -> COMPLETED).
  async function handleAdvanceProcedureStatus(id: number, status?: "CANCELLED") {
    setProcedureActionId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/procedure/advance-status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(status ? { status } : {}),
    });
    setProcedureActionId(null);
    if (afterAction(result, status === "CANCELLED" ? "Booking cancelled" : "Marked completed", "Couldn't update status")) load();
  }
  async function handleApproveProcedureReschedule(id: number) {
    setProcedureActionId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/procedure/reschedule-request/approve`, { method: "POST" });
    setProcedureActionId(null);
    if (afterAction(result, "Reschedule approved", "Couldn't approve reschedule")) load();
  }
  async function handleRejectProcedureReschedule(id: number) {
    setProcedureActionId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/procedure/reschedule-request/reject`, { method: "POST" });
    setProcedureActionId(null);
    if (afterAction(result, "Reschedule rejected", "Couldn't reject reschedule")) load();
  }

  // Item 3 (Spec.md Section 0): soft-delete only, per this project's
  // never-hard-delete convention -- restricted server-side to non-'booked'
  // rows (cancel it first), same guard reflected here by only offering the
  // button once status !== "booked".
  async function handleDelete(id: number) {
    if (!window.confirm("Delete this appointment record? This can't be undone from the portal.")) return;
    setDeletingId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/delete`, { method: "POST" });
    setDeletingId(null);
    if (result.ok) load();
  }

  // Bulk select + delete, same shape as usePatients' row checkboxes + "Delete
  // selected" action -- restricted to non-'booked' rows since that's the same
  // guard the single-row delete button (and the backend) already enforces;
  // a 'booked' appointment can't be selected at all rather than silently
  // failing once "Delete selected" is clicked.
  const toggleSelected = (id: number, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const deletableAppointments = (appointments ?? []).filter((a) => a.status !== "booked");

  const toggleSelectAll = (checked: boolean) => {
    setSelected(checked ? new Set(deletableAppointments.map((a) => a.id)) : new Set());
  };

  const runBulkDelete = async (targets: Appointment[]) => {
    setBulkDeleting(true);
    const result = await portalFetch("/api/portal/bookings/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ appointment_ids: targets.map((a) => a.id) }),
    });
    setBulkDeleting(false);
    setPendingDelete(null);
    if (!result.ok) {
      if (result.unauthorized) router.push("/portal/login");
      else setMutationError(result.error);
      return;
    }
    const deletedIds = new Set((result.data as { deleted: number[] }).deleted);
    load();
    setSelected((prev) => {
      const next = new Set(prev);
      deletedIds.forEach((id) => next.delete(id));
      return next;
    });
  };

  function openCancelPanel(id: number) {
    setReschedulePanelId(null);
    setCancelPanelId(id);
    setCancelMessage(DEFAULT_CANCEL_MESSAGE);
  }

  function closeCancelPanel() {
    setCancelPanelId(null);
  }

  async function handleCancel(id: number) {
    setCancellingId(id);
    const result = await portalFetch(`/api/portal/bookings/${id}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: cancelMessage.trim() }),
    });
    setCancellingId(null);
    setCancelPanelId(null);
    if (result.ok) load();
  }

  async function openReschedulePanel(id: number) {
    setCancelPanelId(null);
    setReschedulePanelId(id);
    setRescheduleMessage(DEFAULT_RESCHEDULE_MESSAGE);
    setRescheduleErrors([]);
    const appointment = appointments?.find((a) => a.id === id);
    const doctorId = appointment?.doctor_id || "";
    const resourceId = appointment?.diagnostic_test_id != null ? String(appointment.diagnostic_test_id) : "";
    setRDoctorId(doctorId);
    setRResourceId(resourceId);
    setRDate("");
    setRSlotId("");
    // Fixed to THIS appointment's own doctor/resource (never user-picked --
    // see rDoctorId/rResourceId's own comment above), so only that one
    // entity's slots are fetched -- not every doctor/resource in the
    // hospital, which /new-booking/context used to eager-load for this.
    setRescheduleSlotsByDate(null);
    const slots = await fetchSlotsByDate(router, { doctorId: doctorId || undefined, resourceId: resourceId || undefined });
    setRescheduleSlotsByDate(slots ?? {});
  }

  function closeReschedulePanel() {
    setReschedulePanelId(null);
  }

  async function handleReschedule(id: number) {
    setReschedulingId(id);
    setRescheduleErrors([]);
    const appointment = appointments?.find((a) => a.id === id);
    const result = await portalFetch(`/api/portal/bookings/${id}/reschedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        department_id: appointment?.department_id || "", doctor_id: appointment?.doctor_id || rDoctorId,
        diagnostic_test_id: appointment?.diagnostic_test_id ?? (rResourceId ? Number(rResourceId) : null),
        slot_id: rSlotId, message: rescheduleMessage.trim(),
      }),
    });
    setReschedulingId(null);
    if (!result.ok) {
      if (result.unauthorized) router.push("/portal/login");
      else setRescheduleErrors([result.error]);
      return;
    }
    const data = result.data as { errors?: string[] };
    if (data.errors?.length) {
      setRescheduleErrors(data.errors);
      return;
    }
    setReschedulePanelId(null);
    load();
  }

  // Diagnostic/Lab reschedule follow-up: a resource-bound appointment has no
  // doctor at all -- rResourceId is set instead of rDoctorId when the panel
  // opens (see openReschedulePanel), but rescheduleSlotsByDate already holds
  // whichever one's slots regardless (fetchSlotsByDate takes exactly one).
  const rDatesForDoctor = rescheduleSlotsByDate ? Object.keys(rescheduleSlotsByDate).sort() : [];
  const rSlotsForDate = rDate && rescheduleSlotsByDate ? rescheduleSlotsByDate[rDate] || [] : [];

  const selectedAppointments = deletableAppointments.filter((a) => selected.has(a.id));
  const allSelected = deletableAppointments.length > 0 && selected.size === deletableAppointments.length;

  return {
    appointments, allAppointments, error, load, isFetching,
    page, setPage, total, pageSize: PAGE_SIZE,
    searchQuery, setSearchQuery, statusFilter, setStatusFilter, typeFilter, setTypeFilter,
    labStatusFilter, setLabStatusFilter,
    modeFilter, setModeFilter,
    applyFilters, resetFilters, filtersDirty,
    cancellingId, cancelPanelId, cancelMessage, setCancelMessage, openCancelPanel, closeCancelPanel, handleCancel,
    reschedulePanelId, reschedulingId, rescheduleSlotsByDate, rescheduleErrors, rescheduleMessage, setRescheduleMessage,
    rDate, setRDate, rSlotId, setRSlotId,
    rDatesForDoctor, rSlotsForDate,
    openReschedulePanel, closeReschedulePanel, handleReschedule,
    markingAttendanceId, handleAttendance,
    advancingLabStatusId, handleAdvanceLabStatus,
    procedureActionId, handleApproveProcedureRequest, handleRejectProcedureRequest,
    handleAdvanceProcedureStatus, handleApproveProcedureReschedule, handleRejectProcedureReschedule,
    deletingId, handleDelete,
    selected, toggleSelected, toggleSelectAll, deletableAppointments, selectedAppointments, allSelected,
    pendingDelete, setPendingDelete, bulkDeleting, runBulkDelete,
  };
}
