"use client";

import { useMemo, useState } from "react";
import { Search, Trash2, UserPlus, UserRound, UserRoundCheck, Users } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DataTable } from "@/components/ui/DataTable";
import { FilterSelect } from "@/components/ui/FilterSelect";
import { PageHeader } from "@/components/ui/PageHeader";
import { PermissionGate } from "@/components/portal/PermissionGate";
import { PortalShell } from "@/components/portal/PortalShell";
import { PortalTopBarActions } from "@/components/portal/PortalTopBarActions";
import { StatTile } from "@/components/portal/StatTile";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { NewBookingDialog } from "@/components/portal/NewBookingDialog";
import { formatHeaderDate } from "@/lib/formatDate";
import { usePatients, type Patient } from "@/hooks/usePatients";
import { GENDER_LABELS, STATUS_LABELS, createPatientColumns } from "./_components/patients-columns";
import { PatientDetailPanel } from "./_components/PatientDetailPanel";

const PATIENT_STATUS_OPTIONS = Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label }));
const GENDER_OPTIONS = Object.entries(GENDER_LABELS).map(([value, label]) => ({ value, label }));

export default function PortalPatientsPage() {
  const { hospital, ready } = usePortalGuard();
  const {
    patients,
    error,
    load,
    search,
    setSearch,
    selected,
    toggleSelected,
    toggleSelectAll,
    selectedPatients,
    allSelected,
    pendingDelete,
    setPendingDelete,
    deleting,
    runDelete,
    departmentFilter, setDepartmentFilter, statusFilter, setStatusFilter, genderFilter, setGenderFilter,
    departmentOptions, filteredPatients, stats,
  } = usePatients(ready);

  const [selectedPatientId, setSelectedPatientId] = useState<number | null>(null);
  const [bookingOpen, setBookingOpen] = useState(false);

  const selectedPatient: Patient | null =
    (patients ?? []).find((p) => p.id === selectedPatientId) || filteredPatients[0] || null;
  const selectedIndex = selectedPatient ? (patients ?? []).findIndex((p) => p.id === selectedPatient.id) : 0;

  function selectPatient(p: Patient) {
    setSelectedPatientId(p.id);
  }

  const columns = useMemo(
    () =>
      createPatientColumns({
        selected, toggleSelected, toggleSelectAll, allSelected,
        onDelete: (p: Patient) => setPendingDelete([p]),
        onSelect: selectPatient,
      }),
    [selected, toggleSelected, toggleSelectAll, allSelected, setPendingDelete],
  );

  const today = new Date();

  return (
    <PortalShell hospital={hospital} active="patients">
        <PageHeader
          title="Patients"
          description={formatHeaderDate(today)}
          actions={<PortalTopBarActions />}
        />
        {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}

        <div className="mb-space-4 grid grid-cols-1 gap-space-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Total Patients" value={stats.total} deltaPct={null} hint="Live count" icon={Users} />
          <StatTile
            label="New Registrations"
            value={stats.newRegistrations}
            deltaPct={null}
            hint="Last 7 days"
            icon={UserPlus}
          />
          <StatTile
            label="Active Patients"
            value={stats.active}
            deltaPct={null}
            hint={`of ${stats.total} total`}
            icon={UserRoundCheck}
          />
          <StatTile
            label="Follow-up Due"
            value={null}
            deltaPct={null}
            hint="No due-date/recall concept exists yet"
            icon={UserRound}
            tint="clay"
          />
        </div>

        <Card className="p-space-4">
          <div className="mb-space-3 flex flex-wrap items-center justify-between gap-space-3">
            <div>
              <h3 className="text-label font-bold text-ink-900">Patient Management</h3>
              <p className="text-[12px] text-ink-400">View and manage all patients</p>
            </div>
            <div className="flex items-center gap-space-2">
              {selectedPatients.length > 0 && (
                <PermissionGate page="patients" action="delete">
                  <Button
                    variant="secondary"
                    size="md"
                    className="border-error/30 text-error hover:border-error hover:bg-error/10"
                    onClick={() => setPendingDelete(selectedPatients)}
                  >
                    <Trash2 size={15} />
                    Delete selected ({selectedPatients.length})
                  </Button>
                </PermissionGate>
              )}
            </div>
          </div>

          <div className="mb-space-3 w-full flex flex-wrap items-center gap-space-2">
            <div className="relative min-w-50 flex-1">
              <Search size={14} className="absolute top-1/2 left-space-2 -translate-y-1/2 text-ink-400" />
              <input
                type="text"
                placeholder="Search by name, patient ID, phone or email…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-9 w-full rounded-md border border-line bg-card pr-space-2 pl-space-7 text-[12.5px] text-ink-900 outline-none focus:border-brand-400"
              />
            </div>
            <FilterSelect
              value={departmentFilter}
              onChange={setDepartmentFilter}
              allLabel="All Departments"
              options={departmentOptions.map((d) => ({ value: d, label: d }))}
            />
            <FilterSelect
              value={statusFilter}
              onChange={setStatusFilter}
              allLabel="All Status"
              options={PATIENT_STATUS_OPTIONS}
            />
            <FilterSelect
              value={genderFilter}
              onChange={setGenderFilter}
              allLabel="All Genders"
              options={GENDER_OPTIONS}
            />
          </div>

          <div className="grid grid-cols-1 items-start gap-space-4 xl:grid-cols-1">
            <div className="xl:col-span-2">
              <DataTable
                columns={columns}
                data={filteredPatients}
                getRowId={(p) => String(p.id)}
                onRowClick={selectPatient}
                // rowClassName={(p) => (p.id === selectedPatient?.id ? "bg-brand-50" : "")}
                enableColumnVisibility
                tableId="patients"
                pageSize={10}
                pageSizeOptions={[10, 25, 50, 100]}
                loading={!patients}
                emptyMessage={
                  patients && patients.length > 0 ? (
                    "No patients match your filters."
                  ) : (
                    <div className="py-space-2 text-center">
                      <UserRound size={28} className="mx-auto mb-space-2 text-ink-300" />
                      <p className="text-[13px] text-ink-400">
                        {search ? "No patients match that search." : "No patients yet — they appear here after a first booking."}
                      </p>
                    </div>
                  )
                }
              />
            </div>
            {/* <div>
              <PatientDetailPanel
                patient={selectedPatient}
                index={Math.max(selectedIndex, 0)}
                onBookAppointment={() => setBookingOpen(true)}
              />
            </div> */}
          </div>
        </Card>

        <ConfirmDialog
          open={pendingDelete !== null}
          title={pendingDelete && pendingDelete.length > 1 ? `Delete ${pendingDelete.length} patients?` : "Delete patient?"}
          message={
            pendingDelete
              ? `This will permanently delete ${
                  pendingDelete.length > 1 ? `${pendingDelete.length} patient records` : pendingDelete[0].name || pendingDelete[0].phone
                }. This action is irreversible.`
              : ""
          }
          confirmLabel="Delete"
          destructive
          busy={deleting}
          onConfirm={() => pendingDelete && runDelete(pendingDelete)}
          onCancel={() => setPendingDelete(null)}
        />

        <NewBookingDialog
          open={bookingOpen}
          onOpenChange={setBookingOpen}
          onBooked={() => load(search)}
          initialPatientName={selectedPatient?.name ?? undefined}
          initialPatientPhone={selectedPatient?.phone ?? undefined}
        />
    </PortalShell>
  );
}
