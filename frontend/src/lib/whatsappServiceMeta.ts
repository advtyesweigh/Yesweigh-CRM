export type ServiceCategory = "service" | "sales" | "cloud" | "stamping";

export type ConvServiceMeta = {
  service_category?: ServiceCategory | "support" | null;
  service_priority?: string | null;
  hold_since?: string | null;
  hold_clock?: string | null;
  on_hold?: boolean;
};

export function serviceCategoryLabel(cat?: string | null): string | null {
  const c = (cat || "").toLowerCase();
  if (c === "service") return "Service";
  if (c === "cloud") return "Cloud";
  if (c === "stamping") return "Stamping";
  if (c === "sales" || c === "support") return "Sales";
  return null;
}

/** CSS class for wa-svc-cat pill (maps legacy support → sales). */
export function serviceCategoryClass(cat?: string | null): string {
  const c = (cat || "").toLowerCase();
  if (c === "support") return "sales";
  if (c === "service" || c === "sales" || c === "cloud" || c === "stamping") return c;
  return "";
}

export function servicePriorityClass(p?: string | null): string {
  const v = (p || "").toLowerCase();
  if (v === "high") return "high";
  if (v === "medium") return "med";
  if (v === "low") return "low";
  return "";
}

export function holdClockTitle(since?: string | null): string {
  if (!since) return "Awaiting agent reply";
  try {
    return `On hold since ${new Date(since).toLocaleString("en-IN")}`;
  } catch {
    return "Awaiting agent reply";
  }
}