"use client";

import {
  Ban,
  CalendarClock,
  CalendarRange,
  ClipboardList,
  Flag,
  Stethoscope,
  UserPlus,
  Users,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { DashboardActivityFeed } from "@/components/portal/DashboardActivityFeed";
import { DashboardPendingApprovals } from "@/components/portal/DashboardPendingApprovals";
import { DashboardQuickActions } from "@/components/portal/DashboardQuickActions";
import { DashboardStaffAttendance } from "@/components/portal/DashboardStaffAttendance";
import { DepartmentDonut } from "@/components/portal/DepartmentDonut";
import { DoctorDashboardView } from "@/components/portal/DoctorDashboardView";
import { PortalMiniCalendar } from "@/components/portal/PortalMiniCalendar";
import { PortalShell } from "@/components/portal/PortalShell";
import { PortalTopBarActions } from "@/components/portal/PortalTopBarActions";
import { TodaysAppointmentsTable } from "@/components/portal/TodaysAppointmentsTable";
import { StatTile } from "@/components/portal/StatTile";
import { WeeklyTrendChart } from "@/components/portal/WeeklyTrendChart";
import { usePortalDashboard } from "@/hooks/usePortalDashboard";
import { formatHeaderDate } from "@/lib/formatDate";
import { useStaffSession } from "@/lib/staffAuth";

const TIER_LABELS: Record<string, string> = {
  tier1: "Tier 1",
  tier2: "Tier 2",
  tier3: "Tier 3",
};

export default function PortalDashboardPage() {
  // Doctors get their own dashboard content (today's appointments, their own
  // stats) instead of the hospital-wide widgets below -- same shared
  // PortalShell/nav either way, per the RBAC-driven consolidation.
  //
  // useStaffSession (not getStaffSession directly): null on the server AND
  // on the client's own first render, so both agree on rendering
  // HospitalDashboard first -- getStaffSession() itself returns the real
  // session immediately client-side, which for a doctor account used to
  // swap in an entirely different component tree (DoctorDashboardView) on
  // the very first client render than what the server had sent, a much
  // bigger hydration mismatch than a mismatched text node. The real
  // session (and DoctorDashboardView, if applicable) arrives an instant
  // later as a normal client-only update.
  const session = useStaffSession();
  if (session?.role === "doctor") {
    return (
      <PortalShell hospital={session.hospital} active="dashboard">
        <DoctorDashboardView />
      </PortalShell>
    );
  }

  return <HospitalDashboard />;
}

function HospitalDashboard() {
  const { data, error, hospital } = usePortalDashboard();
  const today = new Date();

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-paper px-space-4">
        <p className="text-[14px] text-error">{error}</p>
      </div>
    );
  }

  // Same ±30-day window the department donut already computes -- reused
  // here so the stat tile and the donut's center label always agree.
  const totalAppointments = data
    ? data.department_breakdown.reduce((sum, d) => sum + d.count, 0)
    : null;

  return (
    <PortalShell hospital={hospital} active="dashboard">
      <PageHeader
        title={
          <>
            Admin Dashboard
            {data && (
              <span className="ml-space-2 text-[15px] font-medium text-ink-400">
                (
                {TIER_LABELS[data.hospital.data_tier] ||
                  data.hospital.data_tier}
                )
              </span>
            )}
          </>
        }
        description={formatHeaderDate(today)}
        actions={<PortalTopBarActions />}
      />

      {!data ? (
        <p className="text-[13px] text-ink-400">Loading…</p>
      ) : (
        <>
          <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label="Total appointments"
              value={totalAppointments}
              deltaPct={null}
              hint="Last 30 days (± window)"
              icon={CalendarRange}
            />
            <StatTile
              label="Today's appointments"
              value={data.stats.today_appointments}
              deltaPct={data.stats.today_appointments_delta_pct}
              icon={CalendarClock}
            />
            <StatTile
              label="Active doctors"
              value={data.staffing.active_doctors}
              deltaPct={null}
              hint={`of ${data.staffing.total_doctors} total`}
              icon={Stethoscope}
            />
            <StatTile
              label="Staff on duty"
              value={data.staffing.active_staff}
              deltaPct={null}
              hint={`of ${data.staffing.total_staff} total · active accounts, not attendance`}
              icon={Users}
              tint="clay"
            />
            <StatTile
              label="New patients"
              value={data.stats.new_patients_today}
              deltaPct={data.stats.new_patients_today_delta_pct}
              icon={UserPlus}
            />
            <StatTile
              label="No-shows"
              value={data.stats.no_shows_today}
              deltaPct={data.stats.no_shows_today_delta_pct}
              upIsGood={false}
              icon={Ban}
              tint="error"
            />
            <StatTile
              label="Pending leave requests"
              value={null}
              deltaPct={null}
              hint="No approval workflow yet"
              icon={ClipboardList}
              tint="clay"
            />
            <StatTile
              label="Flagged patients"
              value={null}
              deltaPct={null}
              hint="No flagging workflow yet"
              icon={Flag}
              tint="error"
            />
          </div>

          {/* Direct grid children (no space-y wrapper divs) so components
                flow horizontally, row by row, via CSS Grid's own
                auto-placement -- only TodaysAppointmentsTable/
                DashboardPendingApprovals are widened to 2 columns, every
                other tile stays 1-wide and fills in around them. */}
          <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-3 mb-space-4">
            <WeeklyTrendChart data={data.weekly_counts} className="h-90" />
            <DepartmentDonut
              data={data.department_breakdown}
              className="h-90"
            />
            <DashboardQuickActions className="h-90" />
          </div>

          <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-3">
            {/* Stacked in normal flow (space-y), not separate grid tracks --
                so PendingApprovals/StaffAttendance always sit directly under
                TodaysAppointmentsTable and shift down as it grows, instead
                of sitting in a fixed-height grid row with a gap underneath. */}
            <div className="space-y-space-4 lg:col-span-2">
              <TodaysAppointmentsTable
                appointments={data.today_appointments}
              />
              <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-2">
                <DashboardPendingApprovals className="h-62" />
                <DashboardStaffAttendance className="h-62" />
              </div>
            </div>

            <div className="space-y-space-4">
              <PortalMiniCalendar />
              <DashboardActivityFeed items={data.activity_feed} />
            </div>
          </div>
        </>
      )}
    </PortalShell>
  );
}
