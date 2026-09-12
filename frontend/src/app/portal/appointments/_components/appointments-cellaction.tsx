"use client";

import { Beaker, CalendarClock, Check, Download, Eye, MoreHorizontal, Send, Trash2, Upload, UserX, XCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { PermissionGate } from "@/components/portal/PermissionGate";
import type { Appointment } from "@/hooks/useAppointments";

type AppointmentCellActionProps = {
  appointment: Appointment;
  cancelPanelId: number | null;
  reschedulePanelId: number | null;
  onOpenReschedule: (id: number) => void;
  onOpenCancel: (id: number) => void;
  deletingId: number | null;
  onDelete: (id: number) => void;
  /** Optional -- attendance marking folded into this menu (Mark attended /
   * Mark no-show for a still-'booked' row) so the doctor-appointments table
   * doesn't need its own separate "Visited" column just for this. */
  markingAttendanceId?: number | null;
  onAttendance?: (id: number, attended: boolean) => void;
  /** Optional -- lab_status advancement (Mark Sample Collected / Mark
   * Processing / Mark In Progress), only ever offered for a row that
   * actually has a lab_status (Lab Test AND Diagnostics appointments; a
   * doctor consultation never has one). */
  advancingLabStatusId?: number | null;
  onAdvanceLabStatus?: (id: number) => void;
};

// Lab Test goes booked -> sample_collected -> processing; Diagnostics
// (imaging -- MRI/CT/X-Ray/...) has no physical sample-collection step, so
// it goes straight booked -> processing (mirrors the backend's own
// _DIAGNOSTIC_STATUS_FORWARD in portal/routes/bookings.py). Either way,
// report_ready is never a manual step -- it's set automatically when a
// lab_report document is uploaded against the appointment.
const LAB_STATUS_NEXT_LABEL: Record<string, Record<string, string>> = {
  lab: { booked: "Mark sample collected", sample_collected: "Mark processing" },
  diagnostic: { booked: "Mark in progress" },
};

/** Trailing actions cell -- one combined dropdown menu, same pattern as
 * patients-cellaction.tsx: View Details always offered, plus Reschedule/
 * Cancel for a still-'booked' row (hidden while either inline panel is
 * already open for this row), or Delete for a resolved one (Item 3: only
 * ever offered for a non-'booked' appointment, matching the backend's own
 * guard). */
export function AppointmentCellAction({
  appointment: a,
  cancelPanelId,
  reschedulePanelId,
  onOpenReschedule,
  onOpenCancel,
  deletingId,
  onDelete,
  markingAttendanceId,
  onAttendance,
  advancingLabStatusId,
  onAdvanceLabStatus,
}: AppointmentCellActionProps) {
  const router = useRouter();

  if (a.status === "booked" && (cancelPanelId === a.id || reschedulePanelId === a.id)) return null;

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <DropdownMenu>
        <DropdownMenuTrigger
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-ink-600 hover:bg-black/4 hover:text-ink-900"
          aria-label={`Actions for appointment ${a.reference_id || a.id}`}
        >
          <MoreHorizontal size={16} />
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuGroup>
            <DropdownMenuLabel>Actions</DropdownMenuLabel>
            <DropdownMenuItem onClick={() => router.push(`/portal/appointments/${a.id}`)}>
              <Eye size={14} /> View Details
            </DropdownMenuItem>
            {a.lab_status && (
              <>
                <DropdownMenuItem disabled title="Coming soon — no report-download endpoint scoped to a booking yet">
                  <Download size={14} /> Download report
                </DropdownMenuItem>
                <DropdownMenuItem disabled title="Coming soon — report upload exists on a patient's own page, not scoped to a booking yet">
                  <Upload size={14} /> Upload report
                </DropdownMenuItem>
              </>
            )}
            {a.status === "booked" ? (
              <>
                {onAttendance && (
                  <>
                    <DropdownMenuItem disabled={markingAttendanceId === a.id} onClick={() => onAttendance(a.id, true)}>
                      <Check size={14} /> Mark attended
                    </DropdownMenuItem>
                    <DropdownMenuItem disabled={markingAttendanceId === a.id} onClick={() => onAttendance(a.id, false)}>
                      <UserX size={14} /> Mark no-show
                    </DropdownMenuItem>
                  </>
                )}
                {onAdvanceLabStatus && a.lab_status && LAB_STATUS_NEXT_LABEL[a.appointment_type_id || ""]?.[a.lab_status] && (
                  <DropdownMenuItem disabled={advancingLabStatusId === a.id} onClick={() => onAdvanceLabStatus(a.id)}>
                    <Beaker size={14} /> {LAB_STATUS_NEXT_LABEL[a.appointment_type_id || ""][a.lab_status]}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onClick={() => onOpenReschedule(a.id)}>
                  <CalendarClock size={14} /> Reschedule
                </DropdownMenuItem>
                <DropdownMenuItem variant="destructive" onClick={() => onOpenCancel(a.id)}>
                  <XCircle size={14} /> Cancel
                </DropdownMenuItem>
                <DropdownMenuItem disabled title="Coming soon — no reminder backend exists yet">
                  <Send size={14} /> Send reminder
                </DropdownMenuItem>
              </>
            ) : (
              <PermissionGate page="appointments" action="delete">
                <DropdownMenuItem variant="destructive" disabled={deletingId === a.id} onClick={() => onDelete(a.id)}>
                  <Trash2 size={14} /> {deletingId === a.id ? "Deleting…" : "Delete"}
                </DropdownMenuItem>
              </PermissionGate>
            )}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
