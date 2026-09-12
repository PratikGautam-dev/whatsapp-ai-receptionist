"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { CalendarCheck, RotateCcw, Video, type LucideIcon } from "lucide-react";
import { PermissionGate } from "@/components/portal/PermissionGate";
import { AVATAR_TINTS } from "@/lib/avatarTints";
import { cn } from "@/lib/cn";
import { formatShortDateTime } from "@/lib/formatDate";
import { TYPE_LABELS, type Appointment } from "@/hooks/useAppointments";
import { AppointmentCellAction } from "./appointments-cellaction";

export { AVATAR_TINTS };

// Each status gets its own color (same tone vocabulary as Badge.tsx) so
// they read apart at a glance instead of booked/attended and
// cancelled/no_show sharing a color. Exported -- the appointment detail
// page (/portal/appointments/[id]) reuses this same status badge.
export const STATUS_STYLES: Record<string, string> = {
  booked: "bg-brand-50 text-brand-700",
  attended: "bg-success-tint text-success",
  cancelled: "bg-error-tint text-error",
  no_show: "bg-clay-100 text-clay-700",
  rescheduled: "bg-black/[0.04] text-ink-600",
};
export const STATUS_LABELS: Record<string, string> = {
  booked: "Booked",
  cancelled: "Cancelled",
  rescheduled: "Rescheduled",
  attended: "Attended",
  no_show: "No-show",
};
export const SOURCE_LABELS: Record<string, string> = {
  whatsapp: "WhatsApp",
  staff: "Walk-in",
};
// Lab Test Phase 2 follow-up's report lifecycle -- report_ready is never
// advanced from here (only automatically, by uploading a lab_report
// document against the appointment), so it has no "next" label.
export const LAB_STATUS_LABELS: Record<string, string> = {
  booked: "Booked",
  sample_collected: "Sample Collected",
  processing: "Processing",
  report_ready: "Report Ready",
};

type CreateAppointmentColumnsOptions = {
  selected: Set<number>;
  toggleSelected: (id: number, checked: boolean) => void;
  toggleSelectAll: (checked: boolean) => void;
  allSelected: boolean;
  deletableCount: number;
  markingAttendanceId: number | null;
  onAttendance: (id: number, attended: boolean) => void;
  cancelPanelId: number | null;
  reschedulePanelId: number | null;
  onOpenReschedule: (id: number) => void;
  onOpenCancel: (id: number) => void;
  deletingId: number | null;
  onDelete: (id: number) => void;
};

// Doctor-appointment types only (this table is scoped to those, page-level)
// -- a small icon per type so the "Appointment Type" cell reads at a glance,
// same idea as the reference mockup's icon+label cells. Exported so any
// other table showing this same icon+label style (e.g. the dashboard's
// TodaysAppointmentsTable) reuses it rather than defining its own map --
// falls back to CalendarCheck for a type with no icon of its own
// (diagnostic/lab/daycare/second_opinion, which never appear on this
// doctor-appointments-only page but can elsewhere).
export const TYPE_ICONS: Record<string, LucideIcon> = {
  new: CalendarCheck,
  followup: RotateCcw,
  tele: Video,
};

export function initials(name: string | null, phone: string): string {
  if (!name) return phone.slice(-2);
  const parts = name.trim().split(/\s+/);
  return (
    ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() ||
    phone.slice(-2)
  );
}

/** Column definitions for the /portal/appointments (Doctor appointments)
 * DataTable -- deliberately lean, matching the reference mockup's columns
 * plus a leading Appointment ID (reference_id) column and both Appointment
 * time (scheduled_at) and Booked at (created_at), rather than every field
 * this app tracks (Patient ID/Source stay reachable on the appointment
 * detail page instead). No lab/diagnostic columns -- this table is
 * doctor-appointments-only, so lab_status is always null here anyway.
 * Attendance marking (Mark attended/no-show) moved into the Actions menu
 * instead of its own column. */
export function createAppointmentColumns({
  selected,
  toggleSelected,
  toggleSelectAll,
  allSelected,
  deletableCount,
  markingAttendanceId,
  onAttendance,
  cancelPanelId,
  reschedulePanelId,
  onOpenReschedule,
  onOpenCancel,
  deletingId,
  onDelete,
}: CreateAppointmentColumnsOptions): ColumnDef<Appointment>[] {
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
            aria-label="Select all deletable appointments"
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
              aria-label={`Select appointment ${a.reference_id || a.id}`}
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
              {a.patient_name && (
                <p className="truncate text-[11.5px] text-ink-400">{a.phone}</p>
              )}
            </div>
          </div>
        );
      },
    },
    {
      id: "doctor_name",
      header: "Doctor",
      cell: ({ row }) => (
        <span className="text-ink-600">{row.original.doctor_name || "—"}</span>
      ),
    },
    {
      id: "department_name",
      header: "Department",
      cell: ({ row }) => (
        <span className="text-ink-600">
          {row.original.department_name || "—"}
        </span>
      ),
    },
    {
      id: "type",
      header: "Appointment type",
      cell: ({ row }) => {
        const a = row.original;
        const Icon =
          (a.appointment_type_id && TYPE_ICONS[a.appointment_type_id]) ||
          CalendarCheck;
        return (
          <span className="inline-flex items-center gap-space-2 text-ink-600">
            <Icon size={14} strokeWidth={2} className="shrink-0 text-ink-400" />
            {a.appointment_type_id
              ? TYPE_LABELS[a.appointment_type_id] || a.appointment_type_id
              : "Consultation"}
          </span>
        );
      },
    },
    {
      id: "status",
      header: "Status",
      cell: ({ row }) => (
        <span
          className={cn(
            "rounded-full px-space-2 py-0.5 text-[11px] font-semibold",
            STATUS_STYLES[row.original.status] || "bg-black/4 text-ink-600",
          )}
        >
          {STATUS_LABELS[row.original.status] || row.original.status}
        </span>
      ),
    },
    {
      id: "mode",
      header: "Mode",
      cell: ({ row }) => {
        const a = row.original;
        // Real, derived from appointment_type_id/video_link -- there's no
        // room-assignment concept anywhere in this schema, so unlike the
        // mockup's "Room / Mode" column, this never shows a room.
        if (a.appointment_type_id === "tele") {
          return a.video_link ? (
            <a
              href={a.video_link}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[12.5px] font-semibold text-brand-600 hover:underline"
            >
              Video · Join
            </a>
          ) : (
            <span className="text-[12.5px] text-ink-600">Video</span>
          );
        }
        return <span className="text-[12.5px] text-ink-600">In-person</span>;
      },
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
        />
      ),
    },
  ];
}
