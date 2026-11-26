export const CAMPAIGNS_ONLY_ROLES = new Set(["campaigns_only", "media_designer"]);

export function normRole(role: string | undefined): string {
  return (role || "").trim().toLowerCase().replace(/\s+/g, "_");
}

export function isCampaignsOnlyRole(role: string | undefined): boolean {
  return CAMPAIGNS_ONLY_ROLES.has(normRole(role));
}

export function homePathForRole(role: string | undefined): string {
  return isCampaignsOnlyRole(role) ? "/campaigns" : "/";
}

/** Paths allowed when role is campaigns-only (SPA routes). */
export function spaPathAllowedForRole(pathname: string, role: string | undefined): boolean {
  const p = pathname.replace(/\/+$/, "") || "/";
  if (!isCampaignsOnlyRole(role)) return true;
  return p === "/campaigns";
}
