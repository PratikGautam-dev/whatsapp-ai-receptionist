"use client";

import { useMemo, useState } from "react";
import { BedDouble, CalendarPlus, CheckCircle2, Hourglass, IndianRupee } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { DataTable } from "@/components/ui/DataTable";
import { FilterSelect } from "@/components/ui/FilterSelect";
import { PageHeader } from "@/components/ui/PageHeader";
import { NewDaycareBookingDialog } from "@/components/portal/NewDaycareBookingDialog";
import { PortalMiniCalendar } from "@/components/portal/PortalMiniCalendar";
import { PortalShell } from "@/components/portal/PortalShell";
import { PortalTopBarActions } from "@/components/portal/PortalTopBarActions";
import { QuickActions, type QuickAction } from "@/components/portal/QuickActions";
import { StatTile } from "@/components/portal/StatTile";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { cn } from "@/lib/cn";
import { formatHeaderDate } from "@/lib/formatDate";
import { type Appointment, useAppointments } from "@/hooks/useAppointments";
import { createDaycareAppointmentColumns, PROCEDURE_STATUS_LABELS } from "../_components/daycare-appointments-columns";

// Same All/Today/Upcoming/Previous tab set the Doctor and Diagnostic & lab
// appointments pages use, in place of the old procedure_status-grouped tabs
// (Pending approval/Confirmed/Completed/Cancelled — still reachable below
// via the Status filter, which already lists every procedure_status
// individually). Unlike those two pages, this one has no base
// booked/attended/cancelled `status` to key off -- set_procedure_status()
// never touches that column, so matchesTab reads procedure_status instead:
// "upcoming" means CONFIRMED with a real future slot (an APPROVED/REQUESTED/
// UNDER_REVIEW row's scheduled_at is still just the placeholder request
// time, per this file's own AWAITING_SLOT_STATUSES), "previous" means
// COMPLETED (mirrors the other pages' "previous" = attended-only, not a
// merge with cancelled/rejected).
type Tab = "all" | "today" | "upcoming" | "previous";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "today", label: "Today" },
  { id: "upcoming", label: "Upcoming" },
  { id: "previous", label: "Previous" },
];

const STATUS_FILTER_OPTIONS = Object.entries(PROCEDURE_STATUS_LABELS).map(([value, label]) => ({ value, label }));

function isSameDate(iso: string, ref: Date): boolean {
  const d = new Date(iso);
  return d.getFullYear() === ref.getFullYear() && d.getMonth() === ref.getMonth() && d.getDate() === ref.getDate();
}

function matchesTab(a: Appointment, tab: Tab, now: Date): boolean {
  switch (tab) {
    case "today": return isSameDate(a.scheduled_at, now);
    case "upcoming": return a.procedure_status === "CONFIRMED" && new Date(a.scheduled_at) > now;
    case "previous": return a.procedure_status === "COMPLETED";
    default: return true;
  }
}

/** /portal/appointments/daycare -- its own separate sidebar section (below
 * Diagnostic & lab, see PortalSidebar.tsx), same page shape as the other
 * appointments pages (stat tiles -> tab pills + search/filter -> table,
 * right-rail queue widget + quick actions + mini calendar) but with fields/
 * columns specific to a daycare/procedure booking rather than a doctor
 * visit or a lab/imaging test. Backed entirely by real, already-existing
 * backend routes (the Daycare/Procedure rebuild's approve/reject/advance-
 * status/reschedule-approval endpoints in portal/routes/bookings.py) which
 * had no portal page wired to them until now.
 *
 * Unlike the other appointments pages, this one reads/filters/paginates
 * entirely from `allAppointments` (the hook's up-to-500-row, category-
 * scoped summary fetch) rather than the server-paginated `appointments`
 * slice -- there's no server-side filter for procedure_status (only the
 * base booked/attended/cancelled `status`, which stays "booked" through
 * the entire request -> approved -> confirmed lifecycle), so tab/status
 * filtering has to happen client-side against the whole scoped list, with
 * DataTable's own built-in client pagination under it. */
export default function PortalDaycareAppointmentsPage() {
  const { hospital, ready } = usePortalGuard();
  const [tab, setTab] = useState<Tab>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  const {
    allAppointments, error, load,
    procedureActionId,
    handleApproveProcedureRequest, handleRejectProcedureRequest,
    handleAdvanceProcedureStatus, handleApproveProcedureReschedule, handleRejectProcedureReschedule,
  } = useAppointments(ready, "daycare");
  const [newBookingOpen, setNewBookingOpen] = useState(false);

  const today = new Date();

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

  const stats = useMemo(() => {
    if (!allAppointments) return null;
    return {
      total: allAppointments.length,
      pending: allAppointments.filter((a) => a.procedure_status === "REQUESTED" || a.procedure_status === "UNDER_REVIEW").length,
      confirmed: allAppointments.filter((a) => a.procedure_status === "CONFIRMED").length,
      completed: allAppointments.filter((a) => a.procedure_status === "COMPLETED").length,
      rescheduleRequests: allAppointments.filter((a) => !!a.procedure_reschedule_requested_at).length,
    };
  }, [allAppointments]);

  const filteredRows = useMemo(() => {
    if (!allAppointments) return [];
    const now = new Date();
    const q = searchQuery.trim().toLowerCase();
    return allAppointments.filter((a) => {
      if (!matchesTab(a, tab, now)) return false;
      // "pending" is a synthetic value the two approval-queue shortcuts
      // below set (not a real dropdown option, since STATUS_FILTER_OPTIONS
      // only lists individual procedure_status values) -- it stands in for
      // the old "Pending approval" tab's REQUESTED-or-UNDER_REVIEW union,
      // which no single procedure_status value can express.
      if (statusFilter === "pending") {
        if (a.procedure_status !== "REQUESTED" && a.procedure_status !== "UNDER_REVIEW") return false;
      } else if (statusFilter !== "all" && a.procedure_status !== statusFilter) {
        return false;
      }
      if (!q) return true;
      return (
        (a.patient_name || "").toLowerCase().includes(q) ||
        a.phone.includes(q) ||
        (a.procedure_name || "").toLowerCase().includes(q) ||
        (a.procedure_order_reference || "").toLowerCase().includes(q) ||
        (a.reference_id || "").toLowerCase().includes(q)
      );
    });
  }, [allAppointments, tab, statusFilter, searchQuery]);

  const columns = useMemo(
    () =>
      createDaycareAppointmentColumns({
        procedureActionId,
        onApprove: handleApproveProcedureRequest,
        onReject: handleRejectProcedureRequest,
        onAdvance: handleAdvanceProcedureStatus,
        onApproveReschedule: handleApproveProcedureReschedule,
        onRejectReschedule: handleRejectProcedureReschedule,
      }),
    [
      procedureActionId, handleApproveProcedureRequest, handleRejectProcedureRequest,
      handleAdvanceProcedureStatus, handleApproveProcedureReschedule, handleRejectProcedureReschedule,
    ],
  );

  const quickActions: QuickAction[] = [
    { label: "New daycare booking", icon: CalendarPlus, onClick: () => setNewBookingOpen(true) },
    {
      label: "Review pending requests",
      icon: Hourglass,
      // No "pending" tab anymore -- "all" + the synthetic "pending" status
      // value (see filteredRows above) covers the same REQUESTED-or-
      // UNDER_REVIEW scope the old tab did.
      onClick: () => {
        setTab("all");
        setStatusFilter("pending");
      },
    },
  ];

  return (
    <PortalShell hospital={hospital} active="daycare">
      <PageHeader
        title="Daycare appointments"
        description={formatHeaderDate(today)}
        actions={<PortalTopBarActions />}
      />

      {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}

      <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Total daycare bookings" value={stats?.total ?? null} deltaPct={null} hint="Live count" icon={BedDouble} />
        <StatTile
          label="Pending approval"
          value={stats?.pending ?? null}
          deltaPct={null}
          hint="Requests awaiting a decision"
          icon={Hourglass}
          tint="clay"
        />
        <StatTile
          label="Confirmed"
          value={stats?.confirmed ?? null}
          deltaPct={null}
          hint="Slot picked, resources reserved"
          icon={IndianRupee}
        />
        <StatTile
          label="Completed"
          value={stats?.completed ?? null}
          deltaPct={null}
          hint="Visit finished"
          icon={CheckCircle2}
          tint="brand"
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
            <div className="relative min-w-[220px] flex-1">
              <input
                type="text"
                placeholder="Search by patient name, procedure, or order reference…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="h-10 w-full rounded-md border border-line bg-card px-space-3 text-[13px] text-ink-900 outline-none focus:border-brand-400"
              />
            </div>
            <FilterSelect value={statusFilter} onChange={setStatusFilter} allLabel="All Statuses" options={STATUS_FILTER_OPTIONS} />
          </div>

          <Card className="p-space-4">
            <h3 className="text-label mb-space-3 font-bold text-ink-900">Daycare / procedure bookings</h3>
            <DataTable
              columns={columns}
              data={filteredRows}
              getRowId={(a) => String(a.id)}
              pageSizeOptions={[10, 25, 50]}
              loading={!allAppointments}
              emptyMessage={allAppointments && allAppointments.length > 0 ? "No bookings match your search/filter." : "No daycare bookings yet."}
            />
          </Card>
        </div>

        <div className="space-y-space-4">
          <Card className="p-space-4">
            <div className="mb-space-3 flex items-center justify-between">
              <h3 className="text-label font-bold text-ink-900">Approval queue</h3>
              <button
                type="button"
                onClick={() => {
                  setTab("all");
                  setStatusFilter("pending");
                }}
                className="text-[12px] font-semibold text-brand-600 hover:underline"
              >
                View all →
              </button>
            </div>
            <ul className="space-y-space-2 text-[12.5px]">
              <li className="flex items-center gap-space-2 text-ink-600">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-clay-500" /> Pending approval
                <span className="ml-auto font-semibold text-ink-900">{stats?.pending ?? 0}</span>
              </li>
              <li className="flex items-center gap-space-2 text-ink-600">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-brand-600" /> Confirmed, awaiting visit
                <span className="ml-auto font-semibold text-ink-900">{stats?.confirmed ?? 0}</span>
              </li>
              <li className="flex items-center gap-space-2 text-ink-600">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-clay-700" /> Reschedule requests
                <span className="ml-auto font-semibold text-ink-900">{stats?.rescheduleRequests ?? 0}</span>
              </li>
            </ul>
          </Card>

          <QuickActions actions={quickActions} />

          <PortalMiniCalendar category="daycare" />
        </div>
      </div>

      <NewDaycareBookingDialog open={newBookingOpen} onOpenChange={setNewBookingOpen} onBooked={load} />
    </PortalShell>
  );
}
