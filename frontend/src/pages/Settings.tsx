import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import Adms from "./Adms";
import Employees from "./Employees";
import EvolutionSettings from "./EvolutionSettings";
import GeminiSettings from "./GeminiSettings";
import VpsHealth from "./VpsHealth";
import ZohoSettings from "./ZohoSettings";

export const SETTINGS_TABS = [
  { id: "general", label: "General" },
  { id: "employees", label: "Employees" },
  { id: "adms", label: "ADMS" },
  { id: "evolution", label: "WhatsApp" },
  { id: "vps", label: "VPS Health" },
  { id: "zoho", label: "Zoho" },
  { id: "meta", label: "Meta" },
  { id: "yesone", label: "yesone" },
  { id: "gemini", label: "Gemini" },
] as const;

export type SettingsTabId = (typeof SETTINGS_TABS)[number]["id"];

export function parseSettingsTab(raw: string | null): SettingsTabId {
  const match = SETTINGS_TABS.find((t) => t.id === raw);
  return match?.id ?? "general";
}

function ComingSoonPanel({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <section className="panel" style={{ padding: 20, color: "var(--muted)", lineHeight: 1.6 }}>
      <p style={{ margin: "0 0 10px", color: "var(--ink)", fontWeight: 650 }}>{title}</p>
      {subtitle}
    </section>
  );
}

export default function Settings() {
  const [params] = useSearchParams();
  const tab = useMemo<SettingsTabId>(() => parseSettingsTab(params.get("tab")), [params]);

  return (
    <div className="emp-page">
      {tab === "general" && (
        <ComingSoonPanel
          title="General"
          subtitle="Company profile, integrations and system preferences will expand here."
        />
      )}

      {tab === "employees" && <Employees embedded />}
      {tab === "evolution" && <EvolutionSettings />}
      {tab === "adms" && <Adms embedded />}
      {tab === "vps" && <VpsHealth />}
      {tab === "zoho" && <ZohoSettings />}
      {tab === "meta" && (
        <ComingSoonPanel
          title="Meta"
          subtitle="Coming soon — configure Meta integration credentials (distinct from Meta Ads)."
        />
      )}
      {tab === "yesone" && (
        <ComingSoonPanel title="yesone" subtitle="Coming soon — configure YesOne product integration." />
      )}
      {tab === "gemini" && <GeminiSettings />}
    </div>
  );
}
