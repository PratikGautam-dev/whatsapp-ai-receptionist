import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { portalFetch } from "@/lib/portalAuth";
import { toast } from "@/lib/toast";

export type Patient = {
  id: number;
  phone: string;
  name: string | null;
  patient_display_id: string | null;
  mrn: string | null;
  last_visit: string | null;
  visit_count: number;
  visited_count: number;
  date_of_birth: string | null;
  gender: string | null;
  age: number | null;
  status: "active" | "blocked" | "inactive";
  created_at: string;
  /** Department/doctor of this patient's most recent appointment (any
   * status) -- null for a patient who has never had one booked. */
  department_name: string | null;
  doctor_name: string | null;
  // Possible-duplicate review flag (Section 0 follow-up): stamped once, at
  // creation, when this patient matched another ACTIVE patient on at least
  // 3 of {name, phone, date_of_birth, gender} -- see backend's
  // db/repositories/patients.py _flag_duplicate_if_matches(). Purely
  // informational (no merge/dismiss action yet); duplicate_flag_reason is
  // null exactly when duplicate_of_patient_id is.
  duplicate_of_patient_id: number | null;
  duplicate_flag_reason: string | null;
};

const NEW_REGISTRATION_WINDOW_DAYS = 7;

async function fetchPatients(search: string) {
  return portalFetch(`/api/portal/patients?search=${encodeURIComponent(search)}`);
}

async function deletePatients(patientIds: number[]) {
  return portalFetch("/api/portal/patients/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patient_ids: patientIds }),
  });
}

/** Loads + searches the portal's patients list, and owns row selection and
 * delete (single or bulk) for the /portal/patients page. */
export function usePatients(ready: boolean) {
  const router = useRouter();
  const [patients, setPatients] = useState<Patient[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [pendingDelete, setPendingDelete] = useState<Patient[] | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Client-side filters on top of the already-loaded (search-scoped) list --
  // same "list is small, no extra round trip" reasoning as the Doctors page.
  const [departmentFilter, setDepartmentFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [genderFilter, setGenderFilter] = useState("all");

  const load = useCallback(
    async (query: string) => {
      const result = await fetchPatients(query);
      if (!result.ok) {
        if (result.unauthorized) router.push("/portal/login");
        else setError(result.error);
        return;
      }
      setPatients((result.data as { patients: Patient[] }).patients);
    },
    [router],
  );

  useEffect(() => {
    if (ready) load(search);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, load]);

  useEffect(() => {
    if (!ready) return;
    const t = setTimeout(() => load(search), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const toggleSelected = (id: number, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const toggleSelectAll = (checked: boolean) => {
    setSelected(checked ? new Set((patients ?? []).map((p) => p.id)) : new Set());
  };

  const runDelete = async (targets: Patient[]) => {
    setDeleting(true);
    const result = await deletePatients(targets.map((p) => p.id));
    setDeleting(false);
    setPendingDelete(null);
    if (!result.ok) {
      if (result.unauthorized) router.push("/portal/login");
      else {
        setError(result.error);
        toast.error("Couldn't delete patient" + (targets.length > 1 ? "s" : ""), result.error);
      }
      return;
    }
    const deletedIds = new Set((result.data as { deleted: number[] }).deleted);
    toast.success(deletedIds.size > 1 ? `${deletedIds.size} patients deleted` : "Patient deleted");
    setPatients((prev) => (prev ? prev.filter((p) => !deletedIds.has(p.id)) : prev));
    setSelected((prev) => {
      const next = new Set(prev);
      deletedIds.forEach((id) => next.delete(id));
      return next;
    });
  };

  const selectedPatients = (patients ?? []).filter((p) => selected.has(p.id));
  const allSelected = (patients?.length ?? 0) > 0 && selected.size === patients?.length;

  // Department options are scoped to departments this patient list has
  // actually had a visit in (derived from the real last-visit department on
  // each row) -- not the hospital's full department catalog, which isn't
  // fetched on this page.
  const departmentOptions = useMemo(() => {
    const names = new Set((patients ?? []).map((p) => p.department_name).filter((n): n is string => !!n));
    return [...names].sort();
  }, [patients]);

  const filteredPatients = useMemo(() => {
    return (patients ?? []).filter((p) => {
      if (departmentFilter !== "all" && p.department_name !== departmentFilter) return false;
      if (statusFilter !== "all" && p.status !== statusFilter) return false;
      if (genderFilter !== "all" && p.gender !== genderFilter) return false;
      return true;
    });
  }, [patients, departmentFilter, statusFilter, genderFilter]);

  // Snapshotted on load (not Date.now() inline in the memo below, which
  // would call an impure function during render) -- same reasoning as
  // usePatientDetail's own `now` state.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (patients) setNow(Date.now());
  }, [patients]);

  const stats = useMemo(() => {
    const list = patients ?? [];
    const cutoff = (now ?? 0) - NEW_REGISTRATION_WINDOW_DAYS * 24 * 60 * 60 * 1000;
    return {
      total: list.length,
      active: list.filter((p) => p.status === "active").length,
      newRegistrations: now === null ? 0 : list.filter((p) => p.created_at && new Date(p.created_at).getTime() >= cutoff).length,
    };
  }, [patients, now]);

  return {
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
  };
}
