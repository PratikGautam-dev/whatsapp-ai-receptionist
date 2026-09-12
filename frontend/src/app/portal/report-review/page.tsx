"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ClipboardPlus, Search, XCircle } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card } from "@/components/ui/Card";
import { DataTable } from "@/components/ui/DataTable";
import { FilterSelect } from "@/components/ui/FilterSelect";
import { PageHeader } from "@/components/ui/PageHeader";
import { PortalShell } from "@/components/portal/PortalShell";
import { PortalTopBarActions } from "@/components/portal/PortalTopBarActions";
import { StatTile } from "@/components/portal/StatTile";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { formatHeaderDate } from "@/lib/formatDate";
import {
  INITIAL_MOCK_REPORTS,
  RECENT_REPORT_ACTIVITY,
  REPORTS_BY_TYPE_THIS_MONTH,
  initialStats,
  type MockReport,
} from "./_components/mock-reports";
import { createReportColumns, StatusBadge } from "./_components/report-columns";
import { ReportPreviewPanel } from "./_components/ReportPreviewPanel";

const STATUS_OPTIONS = [
  { value: "Pending", label: "Pending" },
  { value: "Approved", label: "Approved" },
  { value: "Rejected", label: "Rejected" },
];

const PRIORITY_OPTIONS = [
  { value: "Normal", label: "Normal" },
  { value: "High", label: "High" },
  { value: "Urgent", label: "Urgent" },
];

/** /portal/report-review -- frontend-only mock page (no reports table, no
 * upload/review routes exist yet), per the user's own explicit "no backend,
 * only frontend" instruction. All state (reports list, stat tiles, activity
 * feed, by-type chart) is a static local seed the page itself mutates via
 * approve/return/delete/flag -- refreshing the page resets everything. Built
 * from the reference screenshot in full, minus nothing, since the whole
 * page is mock by design here (not a case of a real page missing a few
 * fields). */
export default function ReportReviewPage() {
  const { hospital, ready } = usePortalGuard();
  const [reports, setReports] = useState<MockReport[]>(INITIAL_MOCK_REPORTS);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [priorityFilter, setPriorityFilter] = useState("all");

  const stats = useMemo(() => initialStats(), []);
  const today = new Date();

  const typeOptions = useMemo(
    () => Array.from(new Set(reports.map((r) => r.reportType))).map((t) => ({ value: t, label: t })),
    [reports],
  );

  const filteredReports = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return reports.filter((r) => {
      if (typeFilter !== "all" && r.reportType !== typeFilter) return false;
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (priorityFilter !== "all" && r.priority !== priorityFilter) return false;
      if (!q) return true;
      return r.patientName.toLowerCase().includes(q) || r.reportId.toLowerCase().includes(q);
    });
  }, [reports, searchQuery, typeFilter, statusFilter, priorityFilter]);

  // Auto-selects the first (visible) row, same convention as the Doctors/
  // Staff pages' own detail panels, so the preview never starts empty.
  const selected = reports.find((r) => r.id === selectedId) || filteredReports[0] || null;

  function updateReport(id: number, patch: Partial<MockReport>) {
    setReports((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  function handleApprove(report: MockReport) {
    updateReport(report.id, { status: "Approved" });
  }
  function handleReturn(report: MockReport) {
    updateReport(report.id, { status: "Rejected" });
  }
  function handleDelete(report: MockReport) {
    setReports((prev) => prev.filter((r) => r.id !== report.id));
    if (selectedId === report.id) setSelectedId(null);
  }
  function handleToggleUrgent(report: MockReport) {
    updateReport(report.id, { priority: report.priority === "Urgent" ? "Normal" : "Urgent" });
  }

  return (
    <PortalShell hospital={hospital} active="report-review">
      <PageHeader title="Report Review" description={formatHeaderDate(today)} actions={<PortalTopBarActions />} />

      {!ready ? null : (
        <>
          <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile label="Pending Reviews" value={stats.pendingReviews.value} deltaPct={stats.pendingReviews.deltaPct} icon={ClipboardPlus} tint="clay" />
            <StatTile label="Approved Reports" value={stats.approvedReports.value} deltaPct={stats.approvedReports.deltaPct} icon={CheckCircle2} tint="success" />
            <StatTile label="Rejected Reports" value={stats.rejectedReports.value} deltaPct={stats.rejectedReports.deltaPct} icon={XCircle} tint="error" upIsGood={false} />
            <StatTile label="Urgent Reports" value={stats.urgentReports.value} deltaPct={stats.urgentReports.deltaPct} icon={AlertTriangle} tint="error" upIsGood={false} />
          </div>

          <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <Card className="p-space-4">
                <div className="mb-space-3 flex flex-wrap items-start justify-between gap-space-3">
                  <div>
                    <h3 className="text-label font-bold text-ink-900">Uploaded Reports (Needs Review)</h3>
                    <p className="text-hint mt-space-1">Review, approve or return uploaded diagnostic reports.</p>
                  </div>
                </div>

                <div className="mb-space-3 flex flex-wrap items-center gap-space-3">
                  <FilterSelect value={typeFilter} onChange={setTypeFilter} allLabel="All Report Types" options={typeOptions} />
                  <FilterSelect value={statusFilter} onChange={setStatusFilter} allLabel="All Statuses" options={STATUS_OPTIONS} />
                  <FilterSelect value={priorityFilter} onChange={setPriorityFilter} allLabel="All Priorities" options={PRIORITY_OPTIONS} />
                  <div className="relative min-w-50 flex-1">
                    <Search size={14} className="pointer-events-none absolute left-space-3 top-1/2 -translate-y-1/2 text-ink-400" />
                    <input
                      type="text"
                      placeholder="Search by patient name, report ID…"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      className="h-10 w-full rounded-md border border-line bg-card pl-space-8 pr-space-3 text-[13px] text-ink-900 outline-none focus:border-brand-400"
                    />
                  </div>
                </div>

                <DataTable
                  columns={createReportColumns({
                    onSelect: (r) => setSelectedId(r.id),
                    onApprove: handleApprove,
                    onReturn: handleReturn,
                    onDelete: handleDelete,
                    onToggleUrgent: handleToggleUrgent,
                  })}
                  data={filteredReports}
                  getRowId={(r) => String(r.id)}
                  onRowClick={(r) => setSelectedId(r.id)}
                  rowClassName={(r) => (r.id === selected?.id ? "bg-brand-50" : "")}
                  pageSize={10}
                  pageSizeOptions={[10, 25, 50]}
                  emptyMessage="No reports match your search/filters."
                />
              </Card>

              <div className="mt-space-4 grid grid-cols-1 gap-space-4 md:grid-cols-2">
                <Card className="p-space-4">
                  <div className="mb-space-3 flex items-center justify-between">
                    <h3 className="text-label font-bold text-ink-900">Recent Report Activity</h3>
                    <span className="text-[12px] font-semibold text-ink-400">View all</span>
                  </div>
                  <div className="divide-y divide-line">
                    {RECENT_REPORT_ACTIVITY.map((a) => (
                      <div key={a.id} className="flex items-start justify-between gap-space-2 py-space-2">
                        <div className="min-w-0">
                          <p className="text-[12px] text-ink-400">{a.time}</p>
                          <p className="truncate text-[13px] font-semibold text-ink-900">{a.title}</p>
                          <p className="truncate text-[11.5px] text-ink-400">{a.subtitle}</p>
                        </div>
                        <StatusBadge status={a.status} />
                      </div>
                    ))}
                  </div>
                </Card>

                <Card className="p-space-4">
                  <h3 className="text-label mb-space-3 font-bold text-ink-900">Reports by Type (This Month)</h3>
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart data={REPORTS_BY_TYPE_THIS_MONTH} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
                      <CartesianGrid stroke="#e1e0d9" vertical={false} />
                      <XAxis dataKey="type" tickLine={false} axisLine={{ stroke: "#c3c2b7" }} tick={{ fontSize: 11, fill: "#898781" }} />
                      <YAxis tickLine={false} axisLine={false} tick={{ fontSize: 12, fill: "#898781" }} allowDecimals={false} />
                      <Tooltip cursor={{ fill: "#00949E", fillOpacity: 0.08 }} />
                      <Bar dataKey="count" fill="#00949E" radius={[4, 4, 0, 0]} maxBarSize={36} />
                    </BarChart>
                  </ResponsiveContainer>
                </Card>
              </div>
            </div>

            <div>
              <ReportPreviewPanel report={selected} onApprove={handleApprove} onReturn={handleReturn} />
            </div>
          </div>
        </>
      )}
    </PortalShell>
  );
}
