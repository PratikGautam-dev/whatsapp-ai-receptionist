"use client";

import { Check, Eye, MoreHorizontal, ThumbsDown, ThumbsUp, XCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Appointment } from "@/hooks/useAppointments";

type DaycareCellActionProps = {
  appointment: Appointment;
  procedureActionId: number | null;
  onApprove: (id: number) => void;
  onReject: (id: number, reason?: string) => void;
  onAdvance: (id: number, status?: "CANCELLED") => void;
  onApproveReschedule: (id: number) => void;
  onRejectReschedule: (id: number) => void;
};

const TERMINAL_STATUSES = new Set(["COMPLETED", "CANCELLED", "REJECTED"]);

/** Trailing actions cell for /portal/appointments/daycare -- a procedure
 * booking's actions follow its own procedure_status lifecycle (request ->
 * approve/reject -> patient picks a slot -> confirmed -> completed/
 * cancelled), not the plain booked/cancel/reschedule shape
 * AppointmentCellAction offers every other appointment type. */
export function DaycareCellAction({
  appointment: a,
  procedureActionId,
  onApprove,
  onReject,
  onAdvance,
  onApproveReschedule,
  onRejectReschedule,
}: DaycareCellActionProps) {
  const router = useRouter();
  const status = a.procedure_status || "";
  const busy = procedureActionId === a.id;
  const isPendingApproval = status === "REQUESTED" || status === "UNDER_REVIEW";
  const isTerminal = TERMINAL_STATUSES.has(status);

  function handleReject() {
    const reason = window.prompt("Reason for rejecting this request (optional):", "") ?? undefined;
    onReject(a.id, reason);
  }

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <DropdownMenu>
        <DropdownMenuTrigger
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-ink-600 hover:bg-black/4 hover:text-ink-900"
          aria-label={`Actions for booking ${a.reference_id || a.id}`}
        >
          <MoreHorizontal size={16} />
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuGroup>
            <DropdownMenuLabel>Actions</DropdownMenuLabel>
            <DropdownMenuItem onClick={() => router.push(`/portal/appointments/${a.id}`)}>
              <Eye size={14} /> View Details
            </DropdownMenuItem>

            {isPendingApproval && (
              <>
                <DropdownMenuItem disabled={busy} onClick={() => onApprove(a.id)}>
                  <Check size={14} /> Approve request
                </DropdownMenuItem>
                <DropdownMenuItem variant="destructive" disabled={busy} onClick={handleReject}>
                  <XCircle size={14} /> Reject request
                </DropdownMenuItem>
              </>
            )}

            {a.procedure_reschedule_requested_at && (
              <>
                <DropdownMenuItem disabled={busy} onClick={() => onApproveReschedule(a.id)}>
                  <ThumbsUp size={14} /> Approve reschedule
                </DropdownMenuItem>
                <DropdownMenuItem variant="destructive" disabled={busy} onClick={() => onRejectReschedule(a.id)}>
                  <ThumbsDown size={14} /> Reject reschedule
                </DropdownMenuItem>
              </>
            )}

            {status === "CONFIRMED" && (
              <DropdownMenuItem disabled={busy} onClick={() => onAdvance(a.id)}>
                <Check size={14} /> Mark completed
              </DropdownMenuItem>
            )}

            {!isTerminal && (
              <DropdownMenuItem variant="destructive" disabled={busy} onClick={() => onAdvance(a.id, "CANCELLED")}>
                <XCircle size={14} /> Cancel booking
              </DropdownMenuItem>
            )}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
