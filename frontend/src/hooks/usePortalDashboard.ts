import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { portalFetch, type PortalHospital } from "@/lib/portalAuth";
import { useStaffSession } from "@/lib/staffAuth";

export type DashboardData = {
  hospital: PortalHospital;
  stats: {
    today_appointments: number;
    today_appointments_delta_pct: number | null;
    confirmed_today: number;
    confirmed_today_delta_pct: number | null;
    new_patients_today: number;
    new_patients_today_delta_pct: number | null;
    no_shows_today: number;
    no_shows_today_delta_pct: number | null;
    upcoming_appointments: number;
  };
  /** active_staff/total_staff are active-account counts, not a today/"on
   * duty" attendance figure -- no attendance tracking exists. */
  staffing: {
    active_doctors: number;
    total_doctors: number;
    active_staff: number;
    total_staff: number;
  };
  weekly_counts: { date: string; label: string; count: number }[];
  department_breakdown: { department_name: string; count: number }[];
  today_appointments: {
    id: number;
    phone: string;
    patient_name: string | null;
    patient_display_id: string | null;
    department_name: string;
    doctor_name: string;
    scheduled_at: string;
    status: string;
    source: string;
    reference_id: string | null;
    appointment_type_id: string | null;
    video_link: string | null;
  }[];
  activity_feed: {
    label: string;
    phone: string;
    doctor_name: string;
    department_name: string;
    at: string;
  }[];
};

// New bookings (WhatsApp or staff-created) don't push to this tab -- there's
// no websocket/SSE infra in this app -- so poll instead of fetching once on
// mount, otherwise the numbers only ever update on a manual page reload.
// Matches the backend's own dashboard response cache TTL (portal/routes/
// dashboard.py) -- polling faster than that cache refreshes would just be
// re-reading the same cached payload.
const POLL_INTERVAL_MS = 20_000;

/** Loads + polls the /portal/dashboard stats -- backed by the app's single
 * QueryClient (Providers.tsx), not local state, so navigating away and back
 * shows the last-known data instantly (from cache) while a background
 * refetch keeps it current, instead of blanking to "Loading..." and paying
 * a fresh round trip every time (same fix useAppointments.ts already gets
 * from useQuery). */
export function usePortalDashboard() {
  const router = useRouter();
  const session = useStaffSession();

  const { data, error: queryError } = useQuery({
    queryKey: ["portal-dashboard"],
    refetchInterval: POLL_INTERVAL_MS,
    queryFn: async () => {
      const result = await portalFetch("/api/portal/dashboard");
      if (!result.ok) {
        if (result.unauthorized) router.push("/portal/login");
        throw new Error(result.unauthorized ? "Not authenticated." : result.error);
      }
      return result.data as DashboardData;
    },
  });

  return { data: data ?? null, error: queryError ? (queryError as Error).message : null, hospital: session?.hospital ?? null };
}
