"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { PermissionGate } from "@/components/portal/PermissionGate";
import { cn } from "@/lib/cn";
import { formatShortDateTime, formatTimeOnly } from "@/lib/formatDate";
import type { Appointment } from "@/hooks/useAppointments";
import { AppointmentCellAction } from "./appointments-cellaction";
import {
  AVATAR_TINTS,
  initials,
  STATUS_LABELS,
  STATUS_STYLES,
} from "./appointments-columns";

// Daycare/procedure bookings have their own separate sidebar section
// (category "diagnostic" excludes them server-side) -- this table only ever
// renders diagnostic/lab rows, so no "daycare" entry here.
const TEST_TYPE_LABELS: Record<string, string> = {
  diagnostic: "Diagnostics",
  lab: "Lab Test",
};
const TEST_TYPE_STYLES: Record<string, string> = {
  diagnostic: "bg-brand-50 text-brand-700",
  lab: "bg-clay-100 text-clay-700",
};

// The finer-grained progress a resource-bound (Lab Test OR Diagnostics) row
// can be in before its report is ready -- both categories now share this
// same lab_status column; Diagnostics rows just skip "Sample Collected"
// (no physical sample for an MRI/CT/X-Ray) via the backend's own
// _DIAGNOSTIC_STATUS_FORWARD, going straight booked -> processing. A row
// with no lab_status at all (e.g. one predating this, or a doctor
// consultation) falls back to the plain booked/attended/cancelled status
// below instead of a fabricated stage.
export const LAB_STAGE_LABELS: Record<string, string> = {
  booked: "Pending",
  sample_collected: "Sample Collected",
  processing: "Processing",
  report_ready: "Completed",
};
export const LAB_STAGE_STYLES: Record<string, string> = {
  booked: "bg-clay-100 text-clay-700",
  sample_collected: "bg-brand-50 text-brand-700",
  processing: "bg-brand-50 text-brand-700",
  report_ready: "bg-success-tint text-success",
};

type CreateDiagnosticColumnsOptions = {
  selected: Set<number>;
  toggleSelected: (id: number, checked: boolean) => void;
  toggleSelectAll: (checked: boolean) => void;
  allSelected: boolean;
  deletableCount: number;
  markingAttendanceId: number | null;
  onAttendance: (id: number, attended: boolean) => void;
  advancingLabStatusId: number | null;
  onAdvanceLabStatus: (id: number) => void;
  cancelPanelId: number | null;
  reschedulePanelId: number | null;
  onOpenReschedule: (id: number) => void;
  onOpenCancel: (id: number) => void;
  deletingId: number | null;
  onDelete: (id: number) => void;
};

/** Column definitions for /portal/appointments/diagnostic -- mirrors
 * appointments-columns.tsx's shape (same Appointment rows, same
 * AppointmentCellAction) but for resource-bound (diagnostic/lab) bookings:
 * Test/Procedure instead of Doctor, a Type pill instead of an icon+label,
 * and Booking Status/Lab Status as two separate columns (the
 * base booked/attended/cancelled status, and the independent report-
 * lifecycle stage) instead of one merged "Status" cell. */
export function createDiagnosticAppointmentColumns({
  selected,
  toggleSelected,
  toggleSelectAll,
  allSelected,
  deletableCount,
  markingAttendanceId,
  onAttendance,
  advancingLabStatusId,
  onAdvanceLabStatus,
  cancelPanelId,
  reschedulePanelId,
  onOpenReschedule,
  onOpenCancel,
  deletingId,
  onDelete,
}: CreateDiagnosticColumnsOptions): ColumnDef<Appointment>[] {
  return [
    {
      id: "select",
      enableHiding: false,
      header: () => (
        <PermissionGate page="appointments" action="delete">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={(e) => toggleSelectAll(e.target.checked)}
            disabled={deletableCount === 0}
            className="h-4 w-4 accent-brand-600"
            aria-label="Select all deletable bookings"
          />
        </PermissionGate>
      ),
      cell: ({ row }) => {
        const a = row.original;
        if (a.status === "booked") return null;
        return (
          <PermissionGate page="appointments" action="delete">
            <input
              type="checkbox"
              checked={selected.has(a.id)}
              onChange={(e) => toggleSelected(a.id, e.target.checked)}
              className="h-4 w-4 accent-brand-600"
              aria-label={`Select booking ${a.reference_id || a.id}`}
            />
          </PermissionGate>
        );
      },
    },
    {
      id: "reference_id",
      header: "Appointment ID",
      cell: ({ row }) => (
        <span className="whitespace-nowrap font-mono text-[12px] text-ink-600">
          {row.original.reference_id || "—"}
        </span>
      ),
    },
    {
      id: "scheduled_at",
      header: "Appointment time",
      cell: ({ row }) => (
        <span className="whitespace-nowrap tabular-nums text-ink-600">
          {formatShortDateTime(row.original.scheduled_at)}
        </span>
      ),
    },

    {
      id: "patient",
      header: "Patient",
      cell: ({ row }) => {
        const a = row.original;
        return (
          <div className="flex items-center gap-space-2">
            <span
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-bold",
                AVATAR_TINTS[a.id % AVATAR_TINTS.length],
              )}
            >
              {initials(a.patient_name, a.phone)}
            </span>
            <div className="min-w-0">
              <p className="truncate font-semibold text-ink-900">
                {a.patient_name || a.phone}
              </p>
              <p className="truncate text-[11.5px] text-ink-400">
                {a.patient_display_id || a.phone}
              </p>
            </div>
          </div>
        );
      },
    },
    {
      id: "diagnostic_test_name",
      header: "Test / Procedure",
      cell: ({ row }) => (
        <span className="text-ink-900">
          {row.original.diagnostic_test_name || "—"}
        </span>
      ),
    },
    {
      id: "type",
      header: "Type",
      cell: ({ row }) => {
        const a = row.original;
        const key = a.appointment_type_id || "other";
        return (
          <span
            className={cn(
              "rounded-full px-space-2 py-0.5 text-[11px] font-semibold",
              TEST_TYPE_STYLES[key] || "bg-black/4 text-ink-600",
            )}
          >
            {TEST_TYPE_LABELS[key] || "Other"}
          </span>
        );
      },
    },
    {
      id: "booking_status",
      header: "Booking Status",
      cell: ({ row }) => {
        const a = row.original;
        return (
          <span
            className={cn(
              "rounded-full px-space-2 py-0.5 text-[11px] font-semibold",
              STATUS_STYLES[a.status] || "bg-black/4 text-ink-600",
            )}
          >
            {STATUS_LABELS[a.status] || a.status}
          </span>
        );
      },
    },
    {
      id: "lab_status",
      header: "Lab Status",
      // Independent of Booking Status now (its own column) -- a row keeps
      // showing its report-lifecycle stage here even once cancelled/
      // attended, rather than that column "winning" and hiding it, since a
      // viewer scanning for "is the report ready" shouldn't have to also
      // check what Booking Status says first. "—" for a doctor consultation
      // (which never has a lab_status at all).
      cell: ({ row }) => {
        const a = row.original;
        if (!a.lab_status) return <span className="text-ink-400">—</span>;
        return (
          <span
            className={cn(
              "rounded-full px-space-2 py-0.5 text-[11px] font-semibold",
              LAB_STAGE_STYLES[a.lab_status] || "bg-black/4 text-ink-600",
            )}
          >
            {LAB_STAGE_LABELS[a.lab_status] || a.lab_status}
          </span>
        );
      },
    },
    {
      id: "department_name",
      header: "Assigned department",
      // Real (department_name). Unlike the reference mockup's "Assigned
      // Department / Lab Room", no room-assignment concept exists anywhere
      // in this schema -- not shown, rather than invented.
      cell: ({ row }) => (
        <span className="text-ink-600">
          {row.original.department_name || "—"}
        </span>
      ),
    },
    {
      id: "created_at",
      header: "Booked at",
      cell: ({ row }) => {
        const createdAt = row.original.created_at;
        return (
          <span className="whitespace-nowrap tabular-nums text-ink-600">
            {createdAt ? formatShortDateTime(createdAt) : "—"}
          </span>
        );
      },
    },
    {
      id: "actions",
      enableHiding: false,
      header: "Actions",
      cell: ({ row }) => (
        <AppointmentCellAction
          appointment={row.original}
          cancelPanelId={cancelPanelId}
          reschedulePanelId={reschedulePanelId}
          onOpenReschedule={onOpenReschedule}
          onOpenCancel={onOpenCancel}
          deletingId={deletingId}
          onDelete={onDelete}
          markingAttendanceId={markingAttendanceId}
          onAttendance={onAttendance}
          advancingLabStatusId={advancingLabStatusId}
          onAdvanceLabStatus={onAdvanceLabStatus}
        />
      ),
    },
  ];
}
