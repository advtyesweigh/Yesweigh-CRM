/** Fixed CRM roster order for Attendance / Employees (enrollment codes). */
export const STAFF_DISPLAY_ORDER: Record<string, number> = {
  "3": 1, // Biju
  "18": 2, // Visakh
  "14": 3, // Shibin
  "19": 4, // Safna
  "6": 5, // Shalima
  "20": 6, // Fazal
  "22": 7, // Jeemol
  "23": 8, // Ashima
};

const UNKNOWN_RANK = 9_000;

export function normalizeEmployeeCode(code: string | null | undefined): string {
  const c = String(code || "").trim();
  if (/^\d+$/.test(c)) return String(Number(c));
  return c;
}

export function staffDisplayRank(code: string | null | undefined): number {
  const c = normalizeEmployeeCode(code);
  if (!c) return UNKNOWN_RANK + 1;
  return STAFF_DISPLAY_ORDER[c] ?? UNKNOWN_RANK;
}

export function sortByStaffDisplayOrder<T extends { employee_number?: string; employee_name?: string; name?: string; code?: string; adms_employee_code?: string }>(
  rows: T[],
): T[] {
  return [...rows].sort((a, b) => {
    const codeA = a.employee_number || a.adms_employee_code || a.code || "";
    const codeB = b.employee_number || b.adms_employee_code || b.code || "";
    const rank = staffDisplayRank(codeA) - staffDisplayRank(codeB);
    if (rank !== 0) return rank;
    const nameA = (a.employee_name || a.name || "").toLowerCase();
    const nameB = (b.employee_name || b.name || "").toLowerCase();
    return nameA.localeCompare(nameB);
  });
}
