"use client";

import { useState } from "react";
import { Building2, CalendarX, Plus, Search, SlidersHorizontal, Upload, UserRound, UserRoundCheck } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { DataTable } from "@/components/ui/DataTable";
import { Input } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { PortalShell } from "@/components/portal/PortalShell";
import { PortalTopBarActions } from "@/components/portal/PortalTopBarActions";
import { StatTile } from "@/components/portal/StatTile";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { DoctorScheduleForm } from "@/components/portal/DoctorScheduleForm";
import { DoctorLeaveManager } from "@/components/portal/DoctorLeaveManager";
import { DoctorSlotManager } from "@/components/portal/DoctorSlotManager";
import { DoctorTodayAppointments } from "@/components/portal/DoctorTodayAppointments";
import { DoctorCsvImport } from "@/components/portal/DoctorCsvImport";
import { NewBookingDialog } from "@/components/portal/NewBookingDialog";
import { AddStaffDialog } from "@/components/portal/AddStaffDialog";
import { type Doctor, useDoctors } from "@/hooks/useDoctors";
import { createDoctorColumns } from "./_components/doctors-columns";
import { DoctorDetailPanel } from "./_components/DoctorDetailPanel";

export default function PortalDoctorsPage() {
  const { hospital, ready } = usePortalGuard();
  const [activeTab, setActiveTab] = useState<"doctors" | "departments">("doctors");
  const [selectedDoctorId, setSelectedDoctorId] = useState<string | null>(null);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [bookingOpen, setBookingOpen] = useState(false);
  const [createLoginFor, setCreateLoginFor] = useState<Doctor | null>(null);
  // Backend route guards already 403 the actual mutations for clinic tenants
  // lacking manage_doctors -- this is just a UI convenience so those staff
  // don't hit an error after filling out a form. Fails open (keeps the
  // controls) while hospital hasn't loaded yet, matching PortalSidebar.
  const canManageDoctors = !hospital || hospital.admin_capabilities?.includes("manage_doctors");
  const {
    departments, doctors, onLeaveTodayCount, error, load,
    newDeptName, setNewDeptName, addingDept, handleAddDepartment,
    showDoctorForm, showCsvImport, doctorForm, setDoctorForm, doctorErrors, savingDoctor,
    editingDoctorId, loadingDoctorForEdit,
    openAddDoctorForm, toggleCsvImport, cancelDoctorForm, handleSaveDoctor, handleEditDoctor, handleToggleActive,
    togglingId,
    searchQuery, setSearchQuery, activeFilter, setActiveFilter, filteredDoctors,
  } = useDoctors(ready);

  const selectedDoctor: Doctor | null = doctors.find((d) => d.id === selectedDoctorId) || filteredDoctors[0] || null;
  const selectedIndex = selectedDoctor ? doctors.findIndex((d) => d.id === selectedDoctor.id) : 0;

  function selectDoctor(doc: Doctor) {
    setSelectedDoctorId(doc.id);
    setScheduleOpen(false);
    setLeaveOpen(false);
  }

  const columns = createDoctorColumns({
    onSelect: selectDoctor,
    canManage: canManageDoctors,
    togglingId,
    onToggleActive: handleToggleActive,
    loadingDoctorForEdit,
    onEdit: handleEditDoctor,
  });

  return (
    <PortalShell hospital={hospital} active="doctors">
        <PageHeader
          title="Doctors"
          description="Manage doctors, view profiles, availability and department information."
          actions={
            <>
              <Button variant="secondary" size="md" onClick={() => setActiveTab((t) => (t === "departments" ? "doctors" : "departments"))}>
                {activeTab === "departments" ? "Back to doctors" : "Departments"}
              </Button>
              {canManageDoctors && activeTab === "doctors" && (
                <>
                  <Button variant="secondary" size="md" onClick={toggleCsvImport}>
                    <Upload size={14} /> Bulk import
                  </Button>
                  <Button size="md" onClick={openAddDoctorForm}>
                    <Plus size={14} /> Add doctor
                  </Button>
                </>
              )}
              <PortalTopBarActions />
            </>
          }
        />

        {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}
        {!canManageDoctors && activeTab === "doctors" && (
          <p className="mb-space-4 text-[13px] text-ink-400">
            Doctor and department management isn&apos;t available for your account type. Contact support if you need
            changes made.
          </p>
        )}

        {!departments ? (
          <p className="text-[13px] text-ink-400">Loading…</p>
        ) : activeTab === "departments" ? (
          <Card className="h-fit p-space-4">
            <h3 className="text-label mb-space-3 font-bold text-ink-900">Departments</h3>
            {canManageDoctors && (
              <form onSubmit={handleAddDepartment} className="mb-space-3 flex gap-space-2">
                <Input placeholder="New department" value={newDeptName} onChange={(e) => setNewDeptName(e.target.value)} />
                <Button type="submit" size="md" disabled={addingDept || !newDeptName.trim()}>
                  <Plus size={14} />
                </Button>
              </form>
            )}
            {departments.length === 0 ? (
              <p className="text-[12.5px] text-ink-400">No departments yet.</p>
            ) : (
              <ul className="space-y-space-1">
                {departments.map((d) => (
                  <li key={d.id} className="rounded-md bg-paper px-space-3 py-space-2 text-[13px] text-ink-900">
                    {d.name}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        ) : (
          <>
            <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
              <StatTile label="Total doctors" value={doctors.length} deltaPct={null} hint="Live count" icon={UserRound} />
              <StatTile
                label="Active doctors"
                value={doctors.filter((d) => d.is_active).length}
                deltaPct={null}
                hint={`of ${doctors.length} total`}
                icon={UserRoundCheck}
              />
              <StatTile label="On leave" value={onLeaveTodayCount} deltaPct={null} hint="Today" icon={CalendarX} tint="clay" />
              <StatTile label="Departments covered" value={departments.length} deltaPct={null} hint="Live count" icon={Building2} />
            </div>

            {departments.length === 0 && (
              <Card className="mb-space-4 p-space-4">
                <p className="text-[12.5px] text-ink-400">Add a department first, then you can add doctors to it.</p>
              </Card>
            )}

            {showCsvImport && <div className="mb-space-4"><DoctorCsvImport onImported={() => { load(); }} /></div>}

            {showDoctorForm && (
              <div className="mb-space-4">
                <p className="text-label -mb-space-2 font-bold text-ink-900">
                  {editingDoctorId ? "Edit doctor" : "Add doctor"}
                </p>
                <DoctorScheduleForm
                  departments={departments}
                  value={doctorForm}
                  onChange={setDoctorForm}
                  onSave={handleSaveDoctor}
                  onCancel={cancelDoctorForm}
                  saving={savingDoctor}
                  errors={doctorErrors}
                />
              </div>
            )}

            <div className="grid grid-cols-1 items-start gap-space-4 lg:grid-cols-3">
              <div className="lg:col-span-2">
                <Card className="p-space-4">
                  <h3 className="text-label mb-space-3 font-bold text-ink-900">All doctors ({doctors.length})</h3>
                  {doctors.length > 0 && (
                    <div className="mb-space-3 flex flex-wrap items-center gap-space-3">
                      <div className="relative min-w-[200px] flex-1">
                        <Search size={14} className="pointer-events-none absolute left-space-3 top-1/2 -translate-y-1/2 text-ink-400" />
                        <input
                          type="text"
                          placeholder="Search doctors by name, department or specialization…"
                          value={searchQuery}
                          onChange={(e) => setSearchQuery(e.target.value)}
                          className="h-10 w-full rounded-md border border-line bg-card pl-space-8 pr-space-3 text-[13px] text-ink-900 outline-none focus:border-brand-400"
                        />
                      </div>
                      <select
                        value={activeFilter}
                        onChange={(e) => setActiveFilter(e.target.value)}
                        className="h-10 rounded-md border border-line bg-card px-space-3 text-[13px] text-ink-900"
                      >
                        <option value="all">All doctors</option>
                        <option value="active">Available only</option>
                        <option value="inactive">Unavailable only</option>
                      </select>
                      <Button type="button" variant="secondary" disabled title="Coming soon">
                        <SlidersHorizontal size={14} /> Filters
                      </Button>
                    </div>
                  )}
                  <DataTable
                    columns={columns}
                    data={filteredDoctors}
                    getRowId={(d) => d.id}
                    onRowClick={selectDoctor}
                    rowClassName={(d) => (d.id === selectedDoctor?.id ? "bg-brand-50" : "")}
                    emptyMessage={doctors.length === 0 ? "No doctors yet." : "No doctors match your search/filter."}
                  />
                </Card>
              </div>

              <div>
                <DoctorDetailPanel
                  doctor={selectedDoctor}
                  index={Math.max(selectedIndex, 0)}
                  canManage={canManageDoctors}
                  onEdit={handleEditDoctor}
                  togglingId={togglingId}
                  onToggleActive={handleToggleActive}
                  scheduleOpen={scheduleOpen}
                  onToggleSchedule={() => { setScheduleOpen((v) => !v); setLeaveOpen(false); }}
                  leaveOpen={leaveOpen}
                  onToggleLeave={() => { setLeaveOpen((v) => !v); setScheduleOpen(false); }}
                  onBookAppointment={() => setBookingOpen(true)}
                  onCreateLogin={setCreateLoginFor}
                />
              </div>
            </div>

            {selectedDoctor && scheduleOpen && (
              <Card className="mt-space-4 space-y-space-3 p-space-4">
                <h3 className="text-label font-bold text-ink-900">Dr. {selectedDoctor.name} — schedule</h3>
                <DoctorTodayAppointments doctorId={selectedDoctor.id} />
                <DoctorSlotManager doctorId={selectedDoctor.id} />
              </Card>
            )}

            {selectedDoctor && leaveOpen && (
              <Card className="mt-space-4 p-space-4">
                <h3 className="text-label mb-space-3 font-bold text-ink-900">Dr. {selectedDoctor.name} — leave</h3>
                <DoctorLeaveManager doctorId={selectedDoctor.id} />
              </Card>
            )}
          </>
        )}

        <NewBookingDialog open={bookingOpen} onOpenChange={setBookingOpen} onBooked={load} />
        <AddStaffDialog
          open={createLoginFor !== null}
          onOpenChange={(open) => { if (!open) setCreateLoginFor(null); }}
          onCreated={load}
          presetDoctor={createLoginFor}
        />
    </PortalShell>
  );
}
