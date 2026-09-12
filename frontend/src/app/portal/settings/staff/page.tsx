"use client";

import { useMemo, useState } from "react";
import { Building2, CalendarX, Plus, Search, UserCheck, Users } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { DataTable } from "@/components/ui/DataTable";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Field } from "@/components/ui/Field";
import { FilterSelect } from "@/components/ui/FilterSelect";
import { PageHeader } from "@/components/ui/PageHeader";
import { PasswordInput } from "@/components/ui/PasswordInput";
import { AddStaffDialog } from "@/components/portal/AddStaffDialog";
import { EditStaffDialog } from "@/components/portal/EditStaffDialog";
import { PermissionGate } from "@/components/portal/PermissionGate";
import { PortalShell } from "@/components/portal/PortalShell";
import { PortalTopBarActions } from "@/components/portal/PortalTopBarActions";
import { StatTile } from "@/components/portal/StatTile";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { usePermission } from "@/lib/staffAuth";
import { formatHeaderDate } from "@/lib/formatDate";
import { useDepartments } from "@/hooks/useDepartments";
import { useStaffManagement } from "@/hooks/useStaffManagement";
import { createStaffColumns, type StaffRow } from "./_components/staff-columns";
import { StaffDetailPanel } from "./_components/StaffDetailPanel";

const STATUS_OPTIONS = [
  { value: "active", label: "Active" },
  { value: "inactive", label: "Inactive" },
];

export default function StaffManagementPage() {
  const { hospital, ready } = usePortalGuard();
  const canView = usePermission("staff", "view");
  const canManage = usePermission("staff", "write");
  const departments = useDepartments(ready && canView);

  const {
    staff, error, togglingId, load, handleToggleActive, handleSetAttendance,
    resetPasswordTarget, newPassword, setNewPassword, confirmPassword, setConfirmPassword,
    resetErrors, resetting, openResetPassword, closeResetPassword, handleResetPassword,
  } = useStaffManagement(canView);

  const [addStaffOpen, setAddStaffOpen] = useState(false);
  const [editingStaff, setEditingStaff] = useState<StaffRow | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [departmentFilter, setDepartmentFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");

  const today = new Date();

  const rows: StaffRow[] = useMemo(() => staff || [], [staff]);
  const departmentOptions = useMemo(() => (departments || []).map((d) => ({ value: d.id, label: d.name })), [departments]);

  const filteredRows = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return rows.filter((s) => {
      if (departmentFilter !== "all" && s.department_id !== departmentFilter) return false;
      if (statusFilter !== "all" && (statusFilter === "active") !== s.is_active) return false;
      if (!q) return true;
      return (
        s.name.toLowerCase().includes(q) ||
        s.email.toLowerCase().includes(q) ||
        (s.department_name || "").toLowerCase().includes(q) ||
        (s.phone || "").includes(q)
      );
    });
  }, [rows, searchQuery, departmentFilter, statusFilter]);

  // Auto-selects the first (visible) row when nothing's been explicitly
  // clicked yet -- same convention as the Doctors page's own detail panel --
  // so the detail panel never starts on an empty "select someone" state
  // while the directory has at least one row to show.
  const selected = rows.find((s) => s.id === selectedId) || filteredRows[0] || null;
  const selectedIndex = selected ? rows.findIndex((s) => s.id === selected.id) : 0;

  const onLeaveCount = rows.filter((s) => s.attendance_status === "on_leave").length;
  // department_name, not department_id -- a doctor-role row's department
  // comes via doctor_id -> doctors.department_id (department_id itself is
  // always null there by design), so counting department_id alone would
  // silently ignore every doctor's department.
  const departmentsCovered = new Set(rows.map((s) => s.department_name).filter(Boolean)).size;

  return (
    <PortalShell hospital={hospital} active="staff">
      <PageHeader
        title="Staff"
        description={formatHeaderDate(today)}
        actions={<PortalTopBarActions />}
      />

      {!ready || !canView ? (
        !ready ? null : (
          <p className="text-[13px] text-ink-400">You don&apos;t have access to Staff Management.</p>
        )
      ) : (
        <>
          {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}

          <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile label="Total Staff" value={staff ? staff.length : null} deltaPct={null} hint="Live count" icon={Users} />
            <StatTile
              label="Active Staff"
              value={staff ? staff.filter((s) => s.is_active).length : null}
              deltaPct={null}
              hint={staff ? `of ${staff.length} total` : ""}
              icon={UserCheck}
            />
            <StatTile label="On Leave" value={staff ? onLeaveCount : null} deltaPct={null} hint="Today" icon={CalendarX} tint="clay" />
            <StatTile label="Departments" value={staff ? departmentsCovered : null} deltaPct={null} hint="Live count" icon={Building2} />
          </div>

          <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <Card className="p-space-4">
                <div className="mb-space-3 flex flex-wrap items-start justify-between gap-space-3">
                  <div>
                    <h3 className="text-label font-bold text-ink-900">Staff Directory</h3>
                    <p className="text-hint mt-space-1">Manage hospital staff, view attendance and manage access.</p>
                  </div>
                  <PermissionGate page="staff" action="write">
                    <Button size="md" onClick={() => setAddStaffOpen(true)}>
                      <Plus size={14} /> Add Staff
                    </Button>
                  </PermissionGate>
                </div>

                <div className="mb-space-3 flex flex-wrap items-center gap-space-3">
                  <div className="relative min-w-50 flex-1">
                    <Search size={14} className="pointer-events-none absolute left-space-3 top-1/2 -translate-y-1/2 text-ink-400" />
                    <input
                      type="text"
                      placeholder="Search by name, role, department or phone…"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      className="h-10 w-full rounded-md border border-line bg-card pl-space-8 pr-space-3 text-[13px] text-ink-900 outline-none focus:border-brand-400"
                    />
                  </div>
                  <FilterSelect
                    value={departmentFilter}
                    onChange={setDepartmentFilter}
                    allLabel="All Departments"
                    options={departmentOptions}
                  />
                  <FilterSelect value={statusFilter} onChange={setStatusFilter} allLabel="All Status" options={STATUS_OPTIONS} />
                </div>

                <DataTable
                  columns={createStaffColumns({
                    onSelect: (s) => setSelectedId(s.id),
                    canManage,
                    togglingId,
                    onToggleActive: handleToggleActive,
                    onResetPassword: openResetPassword,
                  })}
                  data={filteredRows}
                  getRowId={(s) => String(s.id)}
                  onRowClick={(s) => setSelectedId(s.id)}
                  rowClassName={(s) => (s.id === selected?.id ? "bg-brand-50" : "")}
                  pageSize={10}
                  pageSizeOptions={[10, 25, 50]}
                  loading={!staff}
                  emptyMessage={staff && staff.length > 0 ? "No staff match your search/filters." : "No staff members yet."}
                />
              </Card>
            </div>

            <div>
              <StaffDetailPanel
                staff={selected}
                index={Math.max(selectedIndex, 0)}
                canManage={canManage}
                onResetPassword={openResetPassword}
                onEdit={setEditingStaff}
                onSetAttendance={handleSetAttendance}
              />
            </div>
          </div>
        </>
      )}

      <AddStaffDialog open={addStaffOpen} onOpenChange={setAddStaffOpen} onCreated={load} />

      <EditStaffDialog
        staff={editingStaff}
        onOpenChange={(open) => { if (!open) setEditingStaff(null); }}
        onSaved={load}
      />

      <Dialog open={resetPasswordTarget !== null} onOpenChange={(open) => { if (!open) closeResetPassword(); }}>
        <DialogContent>
          <DialogTitle>Reset password{resetPasswordTarget ? ` for ${resetPasswordTarget.name}` : ""}</DialogTitle>
          <form onSubmit={handleResetPassword} className="flex flex-col gap-space-3">
            <Field label="New password" htmlFor="reset_new_password" required hint="At least 8 characters.">
              <PasswordInput
                id="reset_new_password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
              />
            </Field>
            <Field label="Confirm new password" htmlFor="reset_confirm_password" required>
              <PasswordInput
                id="reset_confirm_password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
              />
            </Field>
            {resetErrors.length > 0 && (
              <ul className="list-disc pl-space-4 text-[12.5px] font-medium text-error">
                {resetErrors.map((err, i) => (
                  <li key={i}>{err}</li>
                ))}
              </ul>
            )}
            <div className="flex gap-space-2">
              <Button type="submit" disabled={resetting} size="md">
                {resetting ? "Resetting…" : "Reset password"}
              </Button>
              <Button type="button" variant="secondary" size="md" onClick={closeResetPassword} disabled={resetting}>
                Cancel
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </PortalShell>
  );
}
