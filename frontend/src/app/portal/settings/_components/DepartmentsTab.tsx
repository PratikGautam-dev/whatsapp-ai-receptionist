"use client";

import { useEffect, useMemo, useState } from "react";
import { Building2, CheckCircle2, Plus, Search, UserCog, Users } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { DataTable } from "@/components/ui/DataTable";
import { FilterSelect } from "@/components/ui/FilterSelect";
import { StatTile } from "@/components/portal/StatTile";
import { useDepartmentsAdmin, type DepartmentDetail, type DepartmentFields } from "@/hooks/useDepartments";
import { useDoctors } from "@/hooks/useDoctors";
import { staffFetch } from "@/lib/staffAuth";
import { toast } from "@/lib/toast";
import { AssignPersonDialog, type PersonOption } from "./AssignPersonDialog";
import { createDepartmentColumns } from "./department-columns";
import { DepartmentDetailsPanel } from "./DepartmentDetailsPanel";
import { DepartmentFormDialog } from "./DepartmentFormDialog";

const STATUS_OPTIONS = [
  { value: "active", label: "Active" },
  { value: "inactive", label: "Inactive" },
];

// Minimal shape this tab needs from GET /api/portal/staff -- not
// useStaffManagement's full StaffMember (that hook also owns the whole
// Staff page's dialogs/reset-password state, which this tab has no use
// for). Doctors have no department_id here (see staff.py's own check --
// a doctor's department comes from their linked doctor profile), so this
// list is implicitly support-staff-only already.
type StaffPickOption = { id: number; name: string; role: string; department_name: string | null };

export function DepartmentsTab() {
  const { departments, error, reload, createDepartment, updateDepartment, setDepartmentActive, setDepartmentVisibility } =
    useDepartmentsAdmin(true);
  const { doctors, load: reloadDoctors } = useDoctors(true);

  const [staffOptions, setStaffOptions] = useState<StaffPickOption[]>([]);
  useEffect(() => {
    staffFetch("/api/portal/staff").then((result) => {
      if (result.ok) setStaffOptions((result.data as StaffPickOption[]) || []);
    });
  }, []);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  const [formOpen, setFormOpen] = useState(false);
  const [formDepartment, setFormDepartment] = useState<DepartmentDetail | null>(null);
  const [savingForm, setSavingForm] = useState(false);

  const [assignDoctorDepartment, setAssignDoctorDepartment] = useState<DepartmentDetail | null>(null);
  const [assigningDoctor, setAssigningDoctor] = useState(false);

  const [manageStaffDepartment, setManageStaffDepartment] = useState<DepartmentDetail | null>(null);
  const [assigningStaff, setAssigningStaff] = useState(false);

  const [visibilitySaving, setVisibilitySaving] = useState(false);

  const filtered = useMemo(() => {
    if (!departments) return [];
    const q = searchQuery.trim().toLowerCase();
    return departments.filter((d) => {
      if (statusFilter === "active" && !d.is_active) return false;
      if (statusFilter === "inactive" && d.is_active) return false;
      if (!q) return true;
      return d.name.toLowerCase().includes(q) || (d.head_doctor?.name || "").toLowerCase().includes(q);
    });
  }, [departments, searchQuery, statusFilter]);

  const selected = (departments || []).find((d) => d.id === selectedId) || filtered[0] || null;

  const totalDepartments = departments?.length ?? 0;
  const activeDepartments = departments?.filter((d) => d.is_active).length ?? 0;
  const doctorsAssigned = departments?.reduce((sum, d) => sum + d.doctor_count, 0) ?? 0;
  const supportStaffAssigned = departments?.reduce((sum, d) => sum + d.support_staff_count, 0) ?? 0;

  function openAddDialog() {
    setFormDepartment(null);
    setFormOpen(true);
  }

  function openEditDialog(department: DepartmentDetail) {
    setFormDepartment(department);
    setFormOpen(true);
  }

  async function handleFormSubmit(fields: DepartmentFields) {
    setSavingForm(true);
    const ok = formDepartment
      ? await updateDepartment(formDepartment.id, fields)
      : await createDepartment(fields);
    setSavingForm(false);
    if (ok) setFormOpen(false);
  }

  async function handleToggleActive(department: DepartmentDetail) {
    await setDepartmentActive(department.id, !department.is_active);
  }

  async function handleVisibilityChange(
    department: DepartmentDetail,
    field: "show_on_frontend" | "online_booking_enabled" | "whatsapp_booking_enabled",
  ) {
    setVisibilitySaving(true);
    await setDepartmentVisibility(department.id, {
      show_on_frontend: department.show_on_frontend,
      online_booking_enabled: department.online_booking_enabled,
      whatsapp_booking_enabled: department.whatsapp_booking_enabled,
      [field]: !department[field],
    });
    setVisibilitySaving(false);
  }

  // Doctor reassignment needs the doctor's FULL current record first --
  // POST /api/portal/doctors/{id} fully re-validates/replaces every field
  // (same route "Edit doctor" uses), so sending just {department_id} would
  // blank out their specialization/qualification/working hours/etc. This
  // mirrors useDoctors.ts's own handleEditDoctor() fetch-then-resubmit
  // shape exactly, just triggered from this tab instead of the Doctors page.
  async function handleAssignDoctor(doctorId: string) {
    if (!assignDoctorDepartment) return;
    setAssigningDoctor(true);
    const result = await staffFetch(`/api/portal/doctors/${doctorId}`);
    if (!result.ok) {
      setAssigningDoctor(false);
      toast.error("Couldn't load doctor", !result.unauthorized ? result.error : undefined);
      return;
    }
    const full = (result.data as { doctor: Record<string, unknown> }).doctor;
    const body = {
      department_id: assignDoctorDepartment.id,
      name: full.name ?? "",
      specialization: full.specialization ?? "",
      qualification: full.qualification ?? "",
      years_experience: full.years_experience != null ? String(full.years_experience) : "",
      working_days: full.working_days ?? [],
      working_hours: full.working_hours ?? [],
      slot_duration_minutes: full.slot_duration_minutes != null ? String(full.slot_duration_minutes) : "",
      breaks: full.breaks ?? [],
      max_bookings_per_slot: full.max_bookings_per_slot != null ? String(full.max_bookings_per_slot) : "1",
      daily_booking_limit: full.daily_booking_limit != null ? String(full.daily_booking_limit) : "",
      online_quota: full.online_quota != null ? String(full.online_quota) : "",
      walkin_quota: full.walkin_quota != null ? String(full.walkin_quota) : "",
      followup_duration_minutes: full.followup_duration_minutes != null ? String(full.followup_duration_minutes) : "",
      effective_from: full.effective_from ?? "",
      phone: full.phone ?? "",
      employee_id: full.employee_id ?? "",
      location: full.location ?? "",
    };
    const updateResult = await staffFetch(`/api/portal/doctors/${doctorId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setAssigningDoctor(false);
    if (!updateResult.ok) {
      toast.error("Couldn't assign doctor", !updateResult.unauthorized ? updateResult.error : undefined);
      return;
    }
    toast.success("Doctor assigned", `${full.name} → ${assignDoctorDepartment.name}`);
    setAssignDoctorDepartment(null);
    await Promise.all([reload(), reloadDoctors()]);
  }

  // Staff (support-staff) reassignment IS a true partial PATCH -- staff.py
  // reads model_fields_set, so sending only department_id is safe and
  // doesn't touch name/phone/shift/etc.
  async function handleAssignStaff(staffId: string) {
    if (!manageStaffDepartment) return;
    setAssigningStaff(true);
    const result = await staffFetch(`/api/portal/staff/${staffId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ department_id: manageStaffDepartment.id }),
    });
    setAssigningStaff(false);
    if (!result.ok) {
      toast.error("Couldn't assign staff member", !result.unauthorized ? result.error : undefined);
      return;
    }
    toast.success("Staff member assigned", manageStaffDepartment.name);
    setManageStaffDepartment(null);
    const refreshed = await staffFetch("/api/portal/staff");
    if (refreshed.ok) setStaffOptions((refreshed.data as StaffPickOption[]) || []);
    await reload();
  }

  const doctorPeople: PersonOption[] = doctors.map((d) => ({
    id: d.id, label: d.name, sublabel: `${d.specialization || "—"} · currently ${d.department_name}`,
  }));
  const staffPeople: PersonOption[] = staffOptions
    .filter((s) => s.role !== "doctor")
    .map((s) => ({
      id: String(s.id), label: s.name, sublabel: `${s.role} · currently ${s.department_name || "no department"}`,
    }));

  if (error) {
    return (
      <Card className="p-space-6">
        <p className="text-center text-[13px] text-error">{error}</p>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-space-4">
      <div className="grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Total Departments" value={totalDepartments} deltaPct={null} hint="Live count" icon={Building2} tint="brand" />
        <StatTile
          label="Active Departments"
          value={activeDepartments}
          deltaPct={null}
          hint={totalDepartments ? `${Math.round((activeDepartments / totalDepartments) * 100)}% of total` : "—"}
          icon={CheckCircle2}
          tint="success"
        />
        <StatTile label="Doctors Assigned" value={doctorsAssigned} deltaPct={null} hint="Live count" icon={Users} tint="brand" />
        <StatTile label="Support Staff Assigned" value={supportStaffAssigned} deltaPct={null} hint="Live count" icon={UserCog} tint="clay" />
      </div>

      <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Card className="p-space-4">
            <div className="mb-space-3 flex flex-wrap items-start justify-between gap-space-3">
              <div>
                <h3 className="text-label font-bold text-ink-900">Department Directory</h3>
                <p className="text-hint mt-space-1">View and manage all hospital departments</p>
              </div>
              <Button size="md" onClick={openAddDialog}>
                <Plus size={14} /> Add Department
              </Button>
            </div>

            <div className="mb-space-3 flex flex-wrap items-center gap-space-3">
              <div className="relative min-w-50 flex-1">
                <Search size={14} className="pointer-events-none absolute left-space-3 top-1/2 -translate-y-1/2 text-ink-400" />
                <input
                  type="text"
                  placeholder="Search departments, heads, or keywords…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="h-10 w-full rounded-md border border-line bg-card pl-space-8 pr-space-3 text-[13px] text-ink-900 outline-none focus:border-brand-400"
                />
              </div>
              <FilterSelect value={statusFilter} onChange={setStatusFilter} allLabel="All Status" options={STATUS_OPTIONS} />
            </div>

            <DataTable
              columns={createDepartmentColumns({
                onSelect: (d) => setSelectedId(d.id),
                onEdit: openEditDialog,
                onToggleActive: handleToggleActive,
              })}
              data={filtered}
              getRowId={(d) => d.id}
              onRowClick={(d) => setSelectedId(d.id)}
              rowClassName={(d) => (d.id === selected?.id ? "bg-brand-50" : "")}
              pageSize={10}
              pageSizeOptions={[10, 25, 50]}
              loading={!departments}
              emptyMessage={departments && departments.length > 0 ? "No departments match your search/filters." : "No departments yet."}
            />
          </Card>
        </div>

        <div>
          <DepartmentDetailsPanel
            department={selected}
            visibilitySaving={visibilitySaving}
            onEdit={openEditDialog}
            onAssignDoctor={setAssignDoctorDepartment}
            onManageStaff={setManageStaffDepartment}
            onToggleActive={handleToggleActive}
            onVisibilityChange={handleVisibilityChange}
          />
        </div>
      </div>

      <DepartmentFormDialog
        open={formOpen}
        department={formDepartment}
        doctors={doctors}
        saving={savingForm}
        onClose={() => setFormOpen(false)}
        onSubmit={handleFormSubmit}
      />

      <AssignPersonDialog
        open={assignDoctorDepartment !== null}
        title={`Assign Doctor to ${assignDoctorDepartment?.name ?? ""}`}
        people={doctorPeople}
        saving={assigningDoctor}
        onClose={() => setAssignDoctorDepartment(null)}
        onAssign={handleAssignDoctor}
      />

      <AssignPersonDialog
        open={manageStaffDepartment !== null}
        title={`Assign Staff to ${manageStaffDepartment?.name ?? ""}`}
        people={staffPeople}
        saving={assigningStaff}
        onClose={() => setManageStaffDepartment(null)}
        onAssign={handleAssignStaff}
      />
    </div>
  );
}
