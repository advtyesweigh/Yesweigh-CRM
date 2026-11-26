/* Auto-generated India states & districts for Leads filters. */
import data from "./indiaStatesDistricts.json";

export type IndiaGeo = {
  states: string[];
  districtsByState: Record<string, string[]>;
  districtAliases: Record<string, string[]>;
};

export const INDIA_STATES: string[] = data.states;
export const DISTRICTS_BY_STATE: Record<string, string[]> = data.districtsByState;
export const DISTRICT_ALIASES: Record<string, string[]> = data.districtAliases;

export function districtsForState(state: string): string[] {
  if (!state) return [];
  // Canonical CRM label is Keralam (Zoho historically used Kerala too).
  const key = state === "Kerala" || state === "Keralam" ? "Keralam" : state;
  return DISTRICTS_BY_STATE[key] || DISTRICTS_BY_STATE[state] || [];
}

export function districtMatchValues(district: string): string[] {
  if (!district) return [];
  const aliases = DISTRICT_ALIASES[district] || [district];
  return Array.from(new Set([district, ...aliases]));
}
