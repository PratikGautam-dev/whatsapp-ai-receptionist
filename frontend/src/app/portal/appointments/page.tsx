"use client";

import { useMemo, useState } from "react";
import {
  CalendarClock,
  CalendarPlus,
  CalendarRange,
  CheckCircle2,
  Clock,
  FileDown,
  Search,
  Send,
  Trash2,
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
import { NewBookingDialog } from "@/components/portal/NewBookingDialog";
import {
  QuickActions,
  type QuickAction,
} from "@/components/portal/QuickActions";
import { StatTile } from "@/components/portal/StatTile";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { cn } from "@/lib/cn";
import { formatHeaderDate, formatTimeOnly } from "@/lib/formatDate";
import {
  type Appointment,
  TYPE_LABELS,
  useAppointments,
} from "@/hooks/useAppointments";
import {
  createAppointmentColumns,
  STATUS_LABELS,
} from "./_components/appointments-columns";
import { RescheduleDialog } from "./_components/RescheduleDialog";

// This page is doctor-appointments-only (see useAppointments(ready, "doctor")
// below) -- restricted to just those 3 types, same scoping reasoning as that
// hook call's own comment.
const DOCTOR_TYPE_OPTIONS = (["new", "followup", "tele"] as const).map(
  (value) => ({ value, label: TYPE_LABELS[value] }),
);
const APPOINTMENT_STATUS_OPTIONS = Object.entries(STATUS_LABELS).map(
  ([value, label]) => ({ value, label }),
);
// Coarser split on top of DOCTOR_TYPE_OPTIONS -- "Walk-in" groups New +
// Follow-up (both in-person; there's no single appointment_type_id for
// "not tele"), "Tele" is the same "tele" type again but picked as a mode
// rather than a Type-dropdown value. Whenever this isn't "All", the Type
// dropdown below drops "Tele" from its own options (useAppointments'
// resolveTypeParam is what actually combines the two into one query).
const DOCTOR_MODE_OPTIONS = [
  { value: "walk_in", label: "Walk-in Appointment" },
  { value: "tele", label: "Tele Appointment" },
];

type Tab = "all" | "today" | "upcoming" | "previous";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "today", label: "Today" },
  { id: "upcoming", label: "Upcoming" },
  { id: "previous", label: "Previous" },
];

function isSameDate(iso: string, ref: Date): boolean {
  const d = new Date(iso);
  return (
    d.getFullYear() === ref.getFullYear() &&
    d.getMonth() === ref.getMonth() &&
    d.getDate() === ref.getDate()
  );
}

function matchesTab(a: Appointment, tab: Tab, now: Date): boolean {
  switch (tab) {
    case "today":
      return isSameDate(a.scheduled_at, now);
    case "upcoming":
      return a.status === "booked" && new Date(a.scheduled_at) > now;
    case "previous":
      return a.status === "attended";
    default:
      return true;
  }
}

/** Same delta-vs-previous-period shape the dashboard's own stat tiles use
 * (db/repositories/dashboard.py's _delta_pct) -- null on a zero baseline
 * rather than a misleading "+100%". */
function pctDelta(current: number, previous: number): number | null {
  if (previous === 0) return null;
  return Math.round(((current - previous) / previous) * 1000) / 10;
}

export default function PortalAppointmentsPage() {
  const { hospital, ready } = usePortalGuard();
  const [newBookingOpen, setNewBookingOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("all");
  const {
    appointments,
    allAppointments,
    error,
    load,
    page,
    setPage,
    total,
    pageSize,
    searchQuery,
    setSearchQuery,
    statusFilter,
    setStatusFilter,
    typeFilter,
    setTypeFilter,
    modeFilter,
    setModeFilter,
    applyFilters,
    resetFilters,
    filtersDirty,
    cancellingId,
    cancelPanelId,
    cancelMessage,
    setCancelMessage,
    openCancelPanel,
    closeCancelPanel,
    handleCancel,
    reschedulePanelId,
    reschedulingId,
    rescheduleSlotsByDate,
    rescheduleErrors,
    rescheduleMessage,
    setRescheduleMessage,
    rDate,
    setRDate,
    rSlotId,
    setRSlotId,
    rDatesForDoctor,
    rSlotsForDate,
    openReschedulePanel,
    closeReschedulePanel,
    handleReschedule,
    markingAttendanceId,
    handleAttendance,
    deletingId,
    handleDelete,
    selected,
    toggleSelected,
    toggleSelectAll,
    deletableAppointments,
    selectedAppointments,
    allSelected,
    pendingDelete,
    setPendingDelete,
    bulkDeleting,
    runBulkDelete,
    // Scoped to "new"/"followup"/"tele" appointment types only -- diagnostic/
    // lab/daycare and second-opinion (report review) appointments have their
    // own sidebar entries and will get their own page later.
  } = useAppointments(ready, "doctor", tab);

  const today = new Date();

  // "Tele" isn't a valid Type-dropdown pick once a mode is chosen -- the
  // hook's setModeFilter already clears a stale "tele" typeFilter itself,
  // this just keeps the dropdown from offering it in the first place.
  const typeOptionsForMode =
    modeFilter === "all"
      ? DOCTOR_TYPE_OPTIONS
      : DOCTOR_TYPE_OPTIONS.filter((o) => o.value !== "tele");

  // Tab counts + stat tiles + "today's schedule" all read `allAppointments`
  // (the hook's separate full, category-scoped, unpaginated fetch) rather
  // than `appointments` (the table's own current 10-row page) -- they need
  // the whole dataset to compute their numbers from, same as before the
  // table itself became server-paginated.
  const tabCounts = useMemo(() => {
    if (!allAppointments)
      return { all: 0, today: 0, upcoming: 0, completed: 0, cancelled: 0 };
    const now = new Date();
    const counts = {
      all: allAppointments.length,
      today: 0,
      upcoming: 0,
      completed: 0,
      cancelled: 0,
    };
    for (const a of allAppointments) {
      for (const t of TABS) {
        if (t.id !== "all" && matchesTab(a, t.id, now)) counts[t.id]++;
      }
    }
    return counts;
  }, [allAppointments]);

  // Real stats, all derived from the same loaded list -- no backend
  // "pending confirmation" status exists in this app (see that tile below),
  // so that one is the one honest gap here.
  const stats = useMemo(() => {
    if (!allAppointments) return null;
    const now = new Date();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const todayCount = allAppointments.filter((a) =>
      isSameDate(a.scheduled_at, now),
    ).length;
    const yesterdayCount = allAppointments.filter((a) =>
      isSameDate(a.scheduled_at, yesterday),
    ).length;
    return {
      total: allAppointments.length,
      today: todayCount,
      todayDeltaPct: pctDelta(todayCount, yesterdayCount),
      completed: allAppointments.filter((a) => a.status === "attended").length,
    };
  }, [allAppointments]);

  const todaysSchedule = useMemo(() => {
    if (!allAppointments) return [];
    return allAppointments
      .filter(
        (a) => isSameDate(a.scheduled_at, today) && a.status !== "cancelled",
      )
      .sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at))
      .slice(0, 6);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allAppointments]);

  const columns = useMemo(
    () =>
      createAppointmentColumns({
        selected,
        toggleSelected,
        toggleSelectAll,
        allSelected,
        deletableCount: deletableAppointments.length,
        markingAttendanceId,
        onAttendance: handleAttendance,
        cancelPanelId,
        reschedulePanelId,
        onOpenReschedule: openReschedulePanel,
        onOpenCancel: openCancelPanel,
        deletingId,
        onDelete: handleDelete,
      }),
    [
      selected,
      toggleSelected,
      toggleSelectAll,
      allSelected,
      deletableAppointments.length,
      markingAttendanceId,
      handleAttendance,
      cancelPanelId,
      reschedulePanelId,
      openReschedulePanel,
      openCancelPanel,
      deletingId,
      handleDelete,
    ],
  );

  function renderRowDetail(a: Appointment) {
    if (cancelPanelId === a.id) {
      return (
        <div className="rounded-lg border border-line bg-paper p-space-3">
          <label
            htmlFor={`cancel-msg-${a.id}`}
            className="mb-space-2 block text-[12px] font-semibold text-ink-600"
          >
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
              <Send size={13} />{" "}
              {cancellingId === a.id ? "Cancelling…" : "Send & cancel"}
            </Button>
            <Button
              size="md"
              variant="secondary"
              onClick={closeCancelPanel}
              disabled={cancellingId === a.id}
            >
              <X size={13} /> Dismiss
            </Button>
          </div>
        </div>
      );
    }

    return null;
  }

  // Reschedule/Cancel/Send reminder dropped from here -- they're per-
  // appointment actions, not page-level ones, so they live in each row's own
  // Actions menu (appointments-cellaction.tsx) instead of duplicating a
  // disabled "use the row's own action" placeholder here.
  const quickActions: QuickAction[] = [
    {
      label: "Add new appointment",
      icon: CalendarPlus,
      onClick: () => setNewBookingOpen(true),
    },
    {
      label: "Export appointments",
      icon: FileDown,
      disabled: true,
      title: "Coming soon",
    },
  ];

  return (
    <PortalShell hospital={hospital} active="appointments">
      <PageHeader
        title="Doctor appointments"
        description={formatHeaderDate(today)}
        actions={
          <>
            <FilterSelect
              value={modeFilter}
              onChange={setModeFilter}
              allLabel="All Appointments"
              options={DOCTOR_MODE_OPTIONS}
            />
            <PortalTopBarActions />
          </>
        }
      />

      {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}

      <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Total doctor appointments"
          value={stats?.total ?? null}
          deltaPct={null}
          hint="Live count"
          icon={CalendarRange}
        />
        <StatTile
          label="Today's appointments"
          value={stats?.today ?? null}
          deltaPct={stats?.todayDeltaPct ?? null}
          hint="vs yesterday"
          icon={CalendarClock}
        />
        <StatTile
          label="Pending confirmations"
          value={null}
          deltaPct={null}
          hint="No such status exists yet — every booked row already reads Confirmed"
          icon={Clock}
          tint="clay"
        />
        <StatTile
          label="Completed consultations"
          value={stats?.completed ?? null}
          deltaPct={null}
          hint="Attended, all-time in this list"
          icon={CheckCircle2}
          tint="success"
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
                <span
                  className={cn(
                    "ml-space-1 tabular-nums",
                    tab === id ? "text-white/80" : "text-ink-400",
                  )}
                >
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
              <Search
                size={14}
                className="pointer-events-none absolute left-space-3 top-1/2 -translate-y-1/2 text-ink-400"
              />
              <input
                type="text"
                placeholder="Search by patient name, doctor, or ID…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-10 w-full rounded-md border border-line bg-card pl-space-8 pr-space-3 text-[13px] text-ink-900 outline-none focus:border-brand-400"
              />
            </div>
            <FilterSelect
              value={typeFilter}
              onChange={setTypeFilter}
              allLabel="All Types"
              options={typeOptionsForMode}
            />
            <FilterSelect
              value={statusFilter}
              onChange={setStatusFilter}
              allLabel="All Status"
              options={APPOINTMENT_STATUS_OPTIONS}
            />
            <FilterActions
              onApply={applyFilters}
              onReset={resetFilters}
              showReset={
                filtersDirty ||
                !!searchQuery ||
                statusFilter !== "all" ||
                typeFilter !== "all"
              }
            />
          </div>

          <Card className="p-space-4">
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
                  ? "No doctor appointments yet."
                  : "No appointments match your search/filter."
              }
            />
          </Card>
        </div>

        <div className="space-y-space-4">
          <Card className="p-space-4">
            <div className="mb-space-3 flex items-center justify-between">
              <h3 className="text-label font-bold text-ink-900">
                Today&apos;s schedule
              </h3>
              <button
                type="button"
                onClick={() => setTab("today")}
                className="text-[12px] font-semibold text-brand-600 hover:underline"
              >
                View all →
              </button>
            </div>
            {todaysSchedule.length === 0 ? (
              <p className="py-space-2 text-center text-[13px] text-ink-400">
                Nothing scheduled today.
              </p>
            ) : (
              <ul className="space-y-space-3">
                {todaysSchedule.map((a) => (
                  <li
                    key={a.id}
                    className="flex items-start gap-space-3 text-[12.5px]"
                  >
                    <span className="mt-0.5 w-[52px] shrink-0 tabular-nums text-ink-400">
                      {formatTimeOnly(a.scheduled_at)}
                    </span>
                    <div className="min-w-0">
                      <p className="truncate font-semibold text-ink-900">
                        {a.patient_name || a.phone}
                      </p>
                      <p className="truncate text-ink-400">
                        Dr. {a.doctor_name || "—"}
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <QuickActions actions={quickActions} />

          <PortalMiniCalendar category="doctor" />
        </div>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title={
          pendingDelete && pendingDelete.length > 1
            ? `Delete ${pendingDelete.length} appointments?`
            : "Delete appointment?"
        }
        message={
          pendingDelete
            ? `This will permanently delete ${
                pendingDelete.length > 1
                  ? `${pendingDelete.length} appointment records`
                  : `the ${pendingDelete[0].reference_id || "selected"} appointment`
              }. This action is irreversible.`
            : ""
        }
        confirmLabel="Delete"
        destructive
        busy={bulkDeleting}
        onConfirm={() => pendingDelete && runBulkDelete(pendingDelete)}
        onCancel={() => setPendingDelete(null)}
      />

      <NewBookingDialog
        open={newBookingOpen}
        onOpenChange={setNewBookingOpen}
        onBooked={load}
      />

      <RescheduleDialog
        appointment={
          (appointments || []).find((a) => a.id === reschedulePanelId) ?? null
        }
        onOpenChange={(open) => {
          if (!open) closeReschedulePanel();
        }}
        slotsByDate={rescheduleSlotsByDate}
        message={rescheduleMessage}
        setMessage={setRescheduleMessage}
        errors={rescheduleErrors}
        submitting={
          reschedulePanelId !== null && reschedulingId === reschedulePanelId
        }
        date={rDate}
        setDate={setRDate}
        slotId={rSlotId}
        setSlotId={setRSlotId}
        datesForDoctor={rDatesForDoctor}
        slotsForDate={rSlotsForDate}
        onSubmit={() =>
          reschedulePanelId !== null && handleReschedule(reschedulePanelId)
        }
      />
    </PortalShell>
  );
}
