"use client";

import { useMemo, useState } from "react";
import {
  Beaker,
  CalendarPlus,
  ClipboardList,
  FileClock,
  FileText,
  Search,
  Send,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DataTable } from "@/components/ui/DataTable";
import { FilterSelect } from "@/components/ui/FilterSelect";
import { PageHeader } from "@/components/ui/PageHeader";
import { FilterActions } from "@/components/portal/FilterActions";
import { PermissionGate } from "@/components/portal/PermissionGate";
import { PortalMiniCalendar } from "@/components/portal/PortalMiniCalendar";
import { PortalShell } from "@/components/portal/PortalShell";
import { PortalTopBarActions } from "@/components/portal/PortalTopBarActions";
import { NewTestBookingDialog } from "@/components/portal/NewTestBookingDialog";
import { QuickActions, type QuickAction } from "@/components/portal/QuickActions";
import { StatTile } from "@/components/portal/StatTile";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { cn } from "@/lib/cn";
import { formatHeaderDate } from "@/lib/formatDate";
import { type Appointment, TYPE_LABELS, useAppointments } from "@/hooks/useAppointments";
import { STATUS_LABELS } from "../_components/appointments-columns";
import { createDiagnosticAppointmentColumns, LAB_STAGE_LABELS } from "../_components/diagnostic-appointments-columns";
import { RescheduleDialog } from "../_components/RescheduleDialog";

// This page is diagnostic/lab-appointments-only (see useAppointments(ready,
// "diagnostic") below) -- daycare/procedure bookings have their own
// separate sidebar section and are excluded server-side (category
// "diagnostic" no longer includes them), so they're not offered as a Type
// filter option here either.
const TEST_TYPE_OPTIONS = (["diagnostic", "lab"] as const).map((value) => ({ value, label: TYPE_LABELS[value] }));
const APPOINTMENT_STATUS_OPTIONS = Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }));
// Independent of APPOINTMENT_STATUS_OPTIONS above (Booking Status vs. Lab
// Status are now two separate columns/filters, see diagnostic-appointments-
// columns.tsx's own split) -- the report lifecycle a Lab Test/Diagnostics
// row moves through before its report is ready.
const LAB_STATUS_OPTIONS = Object.entries(LAB_STAGE_LABELS).map(([value, label]) => ({ value, label }));

// Same All/Today/Upcoming/Previous tab set the Doctor appointments page
// uses (frontend/src/app/portal/appointments/page.tsx) -- replaces the old,
// more granular Diagnostics/Lab tests/Completed/Pending/Cancelled tabs,
// which are still reachable here, just as the Type/Status FilterSelects
// below rather than their own tab pills (Diagnostics/Lab tests = Type
// filter; Completed/Pending/Cancelled = Status filter).
type Tab = "all" | "today" | "upcoming" | "previous";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "today", label: "Today" },
  { id: "upcoming", label: "Upcoming" },
  { id: "previous", label: "Previous" },
];

function isSameDate(iso: string, ref: Date): boolean {
  const d = new Date(iso);
  return d.getFullYear() === ref.getFullYear() && d.getMonth() === ref.getMonth() && d.getDate() === ref.getDate();
}

function matchesTab(a: Appointment, tab: Tab, now: Date): boolean {
  switch (tab) {
    case "today": return isSameDate(a.scheduled_at, now);
    case "upcoming": return a.status === "booked" && new Date(a.scheduled_at) > now;
    case "previous": return a.status === "attended";
    default: return true;
  }
}

/** Same delta-vs-previous-period shape the other appointment pages use. */
function pctDelta(current: number, previous: number): number | null {
  if (previous === 0) return null;
  return Math.round(((current - previous) / previous) * 1000) / 10;
}

export default function PortalDiagnosticAppointmentsPage() {
  const { hospital, ready } = usePortalGuard();
  const [tab, setTab] = useState<Tab>("all");
  const [newTestBookingOpen, setNewTestBookingOpen] = useState(false);
  const {
    appointments, allAppointments, error, load,
    page, setPage, total, pageSize,
    searchQuery, setSearchQuery,
    statusFilter, setStatusFilter, typeFilter, setTypeFilter,
    labStatusFilter, setLabStatusFilter,
    applyFilters, resetFilters, filtersDirty,
    cancellingId, cancelPanelId, cancelMessage, setCancelMessage, openCancelPanel, closeCancelPanel, handleCancel,
    reschedulePanelId, reschedulingId, rescheduleSlotsByDate, rescheduleErrors, rescheduleMessage, setRescheduleMessage,
    rDate, setRDate, rSlotId, setRSlotId,
    rDatesForDoctor, rSlotsForDate,
    openReschedulePanel, closeReschedulePanel, handleReschedule,
    markingAttendanceId, handleAttendance,
    advancingLabStatusId, handleAdvanceLabStatus,
    deletingId, handleDelete,
    selected, toggleSelected, toggleSelectAll, deletableAppointments, selectedAppointments, allSelected,
    pendingDelete, setPendingDelete, bulkDeleting, runBulkDelete,
    // Scoped to "diagnostic"/"lab"/"daycare" appointment types only --
    // doctor consultations and report-review appointments have their own
    // sidebar entries.
  } = useAppointments(ready, "diagnostic", tab);

  const today = new Date();

  // Tab counts + stat tiles + lab queue all read `allAppointments` (the
  // hook's separate full, category-scoped, unpaginated fetch) rather than
  // `appointments` (the table's own current 10-row page) -- they need the
  // whole dataset to compute their numbers from, same as before the table
  // itself became server-paginated.
  const tabCounts = useMemo(() => {
    if (!allAppointments) return { all: 0, today: 0, upcoming: 0, previous: 0 };
    const now = new Date();
    const counts = { all: allAppointments.length, today: 0, upcoming: 0, previous: 0 };
    for (const a of allAppointments) {
      for (const t of TABS) {
        if (t.id !== "all" && matchesTab(a, t.id, now)) counts[t.id]++;
      }
    }
    return counts;
  }, [allAppointments]);

  // Real stats -- Pending report uploads now covers BOTH Lab Test and
  // Diagnostics bookings: both categories share the same lab_status
  // lifecycle (Diagnostics just skips the "Sample Collected" stage --
  // there's no physical sample for an MRI/CT/X-Ray -- going straight
  // booked -> processing, same backend forward-map the Status column and
  // its row action already branch on).
  const stats = useMemo(() => {
    if (!allAppointments) return null;
    const now = new Date();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const diagnosticsToday = allAppointments.filter((a) => a.appointment_type_id === "diagnostic" && isSameDate(a.scheduled_at, now)).length;
    const diagnosticsYesterday = allAppointments.filter(
      (a) => a.appointment_type_id === "diagnostic" && isSameDate(a.scheduled_at, yesterday),
    ).length;
    const labToday = allAppointments.filter((a) => a.appointment_type_id === "lab" && isSameDate(a.scheduled_at, now)).length;
    const labYesterday = allAppointments.filter((a) => a.appointment_type_id === "lab" && isSameDate(a.scheduled_at, yesterday)).length;
    const pendingReports = allAppointments.filter(
      (a) =>
        (a.appointment_type_id === "lab" || a.appointment_type_id === "diagnostic") &&
        a.status === "booked" && a.lab_status !== "report_ready",
    ).length;
    return {
      total: allAppointments.length,
      diagnosticsToday, diagnosticsDeltaPct: pctDelta(diagnosticsToday, diagnosticsYesterday),
      labToday, labDeltaPct: pctDelta(labToday, labYesterday),
      pendingReports,
    };
  }, [allAppointments]);

  // Real lab-queue breakdown, by lab_status, among today's Lab Test
  // bookings -- deliberately Lab Test-only (unlike the Pending report
  // uploads tile above, which now covers both categories): its stage
  // labels ("Waiting for sample collection", "Sample collected") describe
  // a physical specimen workflow that doesn't apply to Diagnostics/imaging
  // bookings at all (they skip straight from booked to processing), so
  // folding them into this same breakdown would misrepresent them rather
  // than just being an incomplete count.
  const labQueue = useMemo(() => {
    if (!allAppointments) return { waiting: 0, collected: 0, processing: 0, ready: 0, total: 0 };
    const rows = allAppointments.filter((a) => a.appointment_type_id === "lab" && isSameDate(a.scheduled_at, today));
    return {
      waiting: rows.filter((a) => a.lab_status === "booked").length,
      collected: rows.filter((a) => a.lab_status === "sample_collected").length,
      processing: rows.filter((a) => a.lab_status === "processing").length,
      ready: rows.filter((a) => a.lab_status === "report_ready").length,
      total: rows.length,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allAppointments]);

  const columns = useMemo(
    () =>
      createDiagnosticAppointmentColumns({
        selected, toggleSelected, toggleSelectAll, allSelected,
        deletableCount: deletableAppointments.length,
        markingAttendanceId, onAttendance: handleAttendance,
        advancingLabStatusId, onAdvanceLabStatus: handleAdvanceLabStatus,
        cancelPanelId, reschedulePanelId,
        onOpenReschedule: openReschedulePanel, onOpenCancel: openCancelPanel,
        deletingId, onDelete: handleDelete,
      }),
    [
      selected, toggleSelected, toggleSelectAll, allSelected, deletableAppointments.length,
      markingAttendanceId, handleAttendance, advancingLabStatusId, handleAdvanceLabStatus,
      cancelPanelId, reschedulePanelId, openReschedulePanel, openCancelPanel, deletingId, handleDelete,
    ],
  );

  function renderRowDetail(a: Appointment) {
    if (cancelPanelId === a.id) {
      return (
        <div className="rounded-lg border border-line bg-paper p-space-3">
          <label htmlFor={`cancel-msg-${a.id}`} className="mb-space-2 block text-[12px] font-semibold text-ink-600">
            Message to send {a.phone} on WhatsApp
          </label>
          <textarea
            id={`cancel-msg-${a.id}`}
            value={cancelMessage}
            onChange={(e) => setCancelMessage(e.target.value)}
            rows={2}
            className="mb-space-2 h-16 w-full resize-none rounded-md border border-line bg-card px-space-3 py-space-2 text-[13px] text-ink-900 outline-none focus:border-brand-400"
          />
          <div className="flex gap-space-2">
            <Button
              size="md"
              onClick={() => handleCancel(a.id)}
              disabled={cancellingId === a.id}
              className="bg-error hover:bg-error/90 active:bg-error/80"
            >
              <Send size={13} /> {cancellingId === a.id ? "Cancelling…" : "Send & cancel"}
            </Button>
            <Button size="md" variant="secondary" onClick={closeCancelPanel} disabled={cancellingId === a.id}>
              <X size={13} /> Dismiss
            </Button>
          </div>
        </div>
      );
    }

    return null;
  }

  const quickActions: QuickAction[] = [
    { label: "New test booking", icon: CalendarPlus, onClick: () => setNewTestBookingOpen(true) },
    { label: "Generate test report", icon: FileText, disabled: true, title: "Coming soon" },
  ];

  return (
    <PortalShell hospital={hospital} active="diagnostic">
        <PageHeader
          title="Diagnostic & lab test appointments"
          description={formatHeaderDate(today)}
          actions={<PortalTopBarActions />}
        />

        {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}

        <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Total test bookings" value={stats?.total ?? null} deltaPct={null} hint="Live count" icon={Beaker} />
          <StatTile
            label="Diagnostics today"
            value={stats?.diagnosticsToday ?? null}
            deltaPct={stats?.diagnosticsDeltaPct ?? null}
            hint="vs yesterday"
            icon={FileText}
          />
          <StatTile
            label="Lab tests today"
            value={stats?.labToday ?? null}
            deltaPct={stats?.labDeltaPct ?? null}
            hint="vs yesterday"
            icon={ClipboardList}
          />
          <StatTile
            label="Pending report uploads"
            value={stats?.pendingReports ?? null}
            deltaPct={null}
            hint="Lab tests and Diagnostics — anything still booked without a report"
            icon={FileClock}
            tint="clay"
          />
        </div>

        <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-3">
          <div className="space-y-space-4 lg:col-span-2">
            <div className="flex flex-wrap gap-space-2">
              {TABS.map(({ id, label }) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setTab(id)}
                  className={cn(
                    "rounded-full border px-space-3 py-space-1 text-[12.5px] font-semibold transition-colors duration-150",
                    tab === id
                      ? "border-brand-600 bg-brand-600 text-white"
                      : "border-line bg-card text-ink-600 hover:border-brand-300 hover:bg-brand-50",
                  )}
                >
                  {label}
                  <span className={cn("ml-space-1 tabular-nums", tab === id ? "text-white/80" : "text-ink-400")}>
                    {tabCounts[id]}
                  </span>
                </button>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-space-3">
              {selectedAppointments.length > 0 && (
                <PermissionGate page="appointments" action="delete">
                  <Button
                    variant="secondary"
                    size="md"
                    className="border-error/30 text-error hover:border-error hover:bg-error/10"
                    onClick={() => setPendingDelete(selectedAppointments)}
                  >
                    <Trash2 size={15} />
                    Delete selected ({selectedAppointments.length})
                  </Button>
                </PermissionGate>
              )}
              <div className="relative min-w-[220px] flex-1">
                <Search size={14} className="pointer-events-none absolute left-space-3 top-1/2 -translate-y-1/2 text-ink-400" />
                <input
                  type="text"
                  placeholder="Search by patient name, test, or ID…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="h-10 w-full rounded-md border border-line bg-card pl-space-8 pr-space-3 text-[13px] text-ink-900 outline-none focus:border-brand-400"
                />
              </div>
              <FilterSelect value={typeFilter} onChange={setTypeFilter} allLabel="All Types" options={TEST_TYPE_OPTIONS} />
              <FilterSelect
                value={statusFilter}
                onChange={setStatusFilter}
                allLabel="All Status"
                options={APPOINTMENT_STATUS_OPTIONS}
              />
              <FilterSelect
                value={labStatusFilter}
                onChange={setLabStatusFilter}
                allLabel="All Lab Status"
                options={LAB_STATUS_OPTIONS}
              />
              <FilterActions
                onApply={applyFilters}
                onReset={resetFilters}
                showReset={filtersDirty || !!searchQuery || statusFilter !== "all" || typeFilter !== "all" || labStatusFilter !== "all"}
              />
            </div>

            <Card className="p-space-4">
              <h3 className="text-label mb-space-3 font-bold text-ink-900">Test appointments &amp; bookings</h3>
              <DataTable
                columns={columns}
                data={appointments ?? []}
                getRowId={(a) => String(a.id)}
                isRowExpanded={(a) => cancelPanelId === a.id}
                renderRowDetail={renderRowDetail}
                pagination={{ page, limit: pageSize, total }}
                onPageChange={setPage}
                loading={!appointments}
                emptyMessage={
                  allAppointments && allAppointments.length === 0
                    ? "No diagnostic or lab bookings yet."
                    : "No bookings match your search/filter."
                }
              />
            </Card>
          </div>

          <div className="space-y-space-4">
            <Card className="p-space-4">
              <div className="mb-space-3 flex items-center justify-between">
                <h3 className="text-label font-bold text-ink-900">Today&apos;s lab queue</h3>
                <button
                  type="button"
                  // No "lab" tab anymore -- "Today" + the Type filter set to
                  // Lab Test together cover the same "today's lab bookings"
                  // scope the old tab did.
                  onClick={() => {
                    setTab("today");
                    applyFilters({ type: "lab" });
                  }}
                  className="text-[12px] font-semibold text-brand-600 hover:underline"
                >
                  View all →
                </button>
              </div>
              <ul className="space-y-space-2 text-[12.5px]">
                <li className="flex items-center gap-space-2 text-ink-600">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-clay-500" /> Waiting for sample collection
                  <span className="ml-auto font-semibold text-ink-900">{labQueue.waiting}</span>
                </li>
                <li className="flex items-center gap-space-2 text-ink-600">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-brand-600" /> Sample collected
                  <span className="ml-auto font-semibold text-ink-900">{labQueue.collected}</span>
                </li>
                <li className="flex items-center gap-space-2 text-ink-600">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-brand-300" /> In testing
                  <span className="ml-auto font-semibold text-ink-900">{labQueue.processing}</span>
                </li>
                <li className="flex items-center gap-space-2 text-ink-600">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-success" /> Reports ready
                  <span className="ml-auto font-semibold text-ink-900">{labQueue.ready}</span>
                </li>
              </ul>
              <div className="mt-space-3 flex items-center justify-between border-t border-line pt-space-3 text-[12.5px] font-semibold">
                <span className="text-ink-600">Total today</span>
                <span className="text-ink-900">{labQueue.total}</span>
              </div>
            </Card>

            <QuickActions actions={quickActions} />

            <PortalMiniCalendar category="diagnostic" />
          </div>
        </div>

        <ConfirmDialog
          open={pendingDelete !== null}
          title={pendingDelete && pendingDelete.length > 1 ? `Delete ${pendingDelete.length} bookings?` : "Delete booking?"}
          message={
            pendingDelete
              ? `This will permanently delete ${
                  pendingDelete.length > 1 ? `${pendingDelete.length} booking records` : `the ${pendingDelete[0].reference_id || "selected"} booking`
                }. This action is irreversible.`
              : ""
          }
          confirmLabel="Delete"
          destructive
          busy={bulkDeleting}
          onConfirm={() => pendingDelete && runBulkDelete(pendingDelete)}
          onCancel={() => setPendingDelete(null)}
        />

        <NewTestBookingDialog
          open={newTestBookingOpen}
          onOpenChange={setNewTestBookingOpen}
          onBooked={load}
        />

        <RescheduleDialog
          appointment={(appointments || []).find((a) => a.id === reschedulePanelId) ?? null}
          onOpenChange={(open) => { if (!open) closeReschedulePanel(); }}
          slotsByDate={rescheduleSlotsByDate}
          message={rescheduleMessage}
          setMessage={setRescheduleMessage}
          errors={rescheduleErrors}
          submitting={reschedulePanelId !== null && reschedulingId === reschedulePanelId}
          date={rDate}
          setDate={setRDate}
          slotId={rSlotId}
          setSlotId={setRSlotId}
          datesForDoctor={rDatesForDoctor}
          slotsForDate={rSlotsForDate}
          onSubmit={() => reschedulePanelId !== null && handleReschedule(reschedulePanelId)}
        />
    </PortalShell>
  );
}
