import { CSSProperties, FormEvent, MouseEvent, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import IndiaSalesMap from "../components/IndiaSalesMap";
import { INDIA_STATES, districtsForState } from "../data/indiaGeo";
import {
  buildLeadStateTileRows,
  dealerSalesTotalForTile,
  leadStateFilterDisplayName,
  leadStateTileForFilter,
} from "../data/leadStateTiles";
import {
  LEAD_LANGUAGE_MARKETS,
  languageMarketStyleForBlock,
  leadLanguageFilterDisplayName,
  marketLanguageBlockForTile,
} from "../data/leadLanguageMarkets";
import { compareLeadStateTileOrder } from "../data/stateDealerPriority";
import { INDIA_STATE_PATHS, mapStateKey } from "../data/indiaStatePaths";

type Lead = {
  id: number;
  name: string;
  phone: string;
  email: string;
  company: string;
  contact_person: string;
  business_type: string;
  customer_website: string;
  lead_score: number | null;
  city: string;
  district: string;
  state: string;
  pin: string;
  address: string;
  status: string;
  source: string;
  priority: string;
  product_category: string;
  estimated_value: number | string | null;
  campaign: string;
  tags: string;
  next_follow_up: string | null;
  last_contacted: string | null;
  notes: string;
  owner_id: number | null;
  owner_name: string;
};

type LeadPage = {
  items: Lead[];
  total: number;
  page: number;
  page_size: number;
  status_counts?: Record<string, number>;
};

type LeadMeta = {
  owners: { id: number; name: string; role: string }[];
  business_types: string[];
  sources: string[];
  statuses: string[];
  priorities: string[];
  product_categories: string[];
  campaigns: string[];
  tag_suggestions: string[];
};

type LeadStateRow = { state: string; lead_count: number };

type LeadByState = {
  items: LeadStateRow[];
  total: number;
};

type LeadLanguageRow = {
  language: string;
  iso: string;
  label: string;
  lead_count: number;
};

type LeadByLanguage = {
  items: LeadLanguageRow[];
  total: number;
  with_language: number;
};

type SalesByStateOut = {
  year: number;
  items: { state: string; sales_total: number }[];
};

/** Dealer ₹ by map state (Kerala → Keralam) for chip ordering. */
function salesTotalsByMapState(items: { state: string; sales_total: number }[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const row of items) {
    const key = mapStateKey(row.state);
    m.set(key, (m.get(key) ?? 0) + row.sales_total);
  }
  return m;
}

type PincodeLookup = {
  pin: string;
  city: string;
  district: string;
  state: string;
  office?: string;
};

type EditForm = {
  name: string;
  company: string;
  phone: string;
  contact_person: string;
  business_type: string;
  customer_website: string;
  lead_score: string;
  source: string;
  owner_id: string;
  status: string;
  priority: string;
  product_category: string;
  estimated_value: string;
  pin: string;
  city: string;
  district: string;
  state: string;
  address: string;
  next_follow_up: string;
  last_contacted: string;
  campaign: string;
  tags: string[];
  notes: string;
};

const PAGE_SIZE = 50;
const NOTES_MAX = 1000;

const STATUS_TILES = [
  { key: "all", label: "All" },
  { key: "new", label: "New" },
  { key: "contacted", label: "Contacted" },
  { key: "qualified", label: "Qualified" },
  { key: "won", label: "Won" },
  { key: "lost", label: "Lost" },
] as const;

const FALLBACK_META: LeadMeta = {
  owners: [],
  business_types: [
    "Weighing Equipment Dealer",
    "Retailer",
    "Distributor",
    "Manufacturer",
    "End User / Industry",
    "Service Provider",
    "Other",
  ],
  sources: [
    "Website Enquiry",
    "WhatsApp",
    "Visiting Card",
    "Phone Call",
    "Google Ads",
    "Referral",
    "Trade Show",
    "Walk-in",
    "LMOMS",
    "manual",
    "Other",
  ],
  statuses: ["new", "contacted", "qualified", "won", "lost"],
  priorities: ["low", "medium", "high", "urgent"],
  product_categories: [
    "Electronic Weighing Scales",
    "Bench Scale",
    "Label Scale",
    "Platform Scale",
    "Price Computing",
    "POS / Billing",
    "Others",
  ],
  campaigns: [
    "Google Ads – Weighing Scales",
    "WhatsApp Outreach",
    "Website Enquiry",
    "Trade Show",
  ],
  tag_suggestions: ["Industrial", "Price Enquiry", "Hot Lead", "Dealer", "Follow-up", "Retail"],
};

type LoadOpts = {
  page?: number;
  q?: string;
  status?: string;
  state?: string;
  district?: string;
  language?: string;
};

export type LeadCreatePrefill = Partial<EditForm>;

type LeadsLocationState = {
  createLead?: LeadCreatePrefill;
};

function emptyForm(): EditForm {
  return {
    name: "",
    company: "",
    phone: "",
    contact_person: "",
    business_type: "",
    customer_website: "",
    lead_score: "",
    source: "Website Enquiry",
    owner_id: "",
    status: "new",
    priority: "medium",
    product_category: "",
    estimated_value: "",
    pin: "",
    city: "",
    district: "",
    state: "",
    address: "",
    next_follow_up: "",
    last_contacted: "",
    campaign: "",
    tags: [],
    notes: "",
  };
}

function parseTags(raw: string | null | undefined): string[] {
  if (!raw) return [];
  return Array.from(
    new Set(
      String(raw)
        .replace(/;/g, ",")
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean)
    )
  );
}

function dateInputValue(raw: string | null | undefined): string {
  if (!raw) return "";
  const s = String(raw).trim();
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  return "";
}

function moneyInputValue(raw: number | string | null | undefined): string {
  if (raw == null || raw === "") return "";
  const n = typeof raw === "number" ? raw : Number(String(raw).replace(/,/g, ""));
  if (!Number.isFinite(n)) return String(raw);
  return String(n);
}

function formFromLead(lead: Lead): EditForm {
  return {
    name: lead.name || "",
    company: lead.company || "",
    phone: lead.phone || "",
    contact_person: lead.contact_person || "",
    business_type: lead.business_type || "",
    customer_website: lead.customer_website || "",
    lead_score:
      lead.lead_score != null && Number.isFinite(Number(lead.lead_score))
        ? String(lead.lead_score)
        : "",
    source: lead.source || "Website Enquiry",
    owner_id: lead.owner_id != null ? String(lead.owner_id) : "",
    status: (lead.status || "new").toLowerCase(),
    priority: (lead.priority || "medium").toLowerCase(),
    product_category: lead.product_category || "",
    estimated_value: moneyInputValue(lead.estimated_value),
    pin: lead.pin || "",
    city: lead.city || "",
    district: lead.district || "",
    state: lead.state === "Kerala" ? "Keralam" : lead.state || "",
    address: lead.address || "",
    next_follow_up: dateInputValue(lead.next_follow_up),
    last_contacted: dateInputValue(lead.last_contacted),
    campaign: lead.campaign || "",
    tags: parseTags(lead.tags),
    notes: lead.notes || "",
  };
}

function titleCaseStatus(s: string): string {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function priorityDot(priority: string): string {
  switch ((priority || "").toLowerCase()) {
    case "low":
      return "#22c55e";
    case "high":
      return "#ef4444";
    case "urgent":
      return "#b91c1c";
    default:
      return "#f59e0b";
  }
}

export default function Leads() {
  const location = useLocation();
  const navigate = useNavigate();
  const [rows, setRows] = useState<Lead[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [stateFilter, setStateFilter] = useState("");
  const [languageFilter, setLanguageFilter] = useState("");
  const [districtFilter, setDistrictFilter] = useState("");
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const [stateLeadStats, setStateLeadStats] = useState<LeadStateRow[]>([]);
  const [stateLeadTotal, setStateLeadTotal] = useState(0);
  const [geoOverviewOpen, setGeoOverviewOpen] = useState(true);
  const [stateStatsLoading, setStateStatsLoading] = useState(true);
  const [languageLeadStats, setLanguageLeadStats] = useState<LeadLanguageRow[]>([]);
  const [languageStatsLoading, setLanguageStatsLoading] = useState(true);
  const [dealerSalesByMapState, setDealerSalesByMapState] = useState<Map<string, number>>(new Map());

  const [editing, setEditing] = useState<Lead | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<EditForm>(emptyForm);
  const [meta, setMeta] = useState<LeadMeta>(FALLBACK_META);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [pinHint, setPinHint] = useState("");
  const [pinLoading, setPinLoading] = useState(false);
  const [hoveredTileMembers, setHoveredTileMembers] = useState<string[] | null>(null);

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const districtOptions = useMemo(() => {
    if (/[|,]/.test(stateFilter)) return [];
    return districtsForState(stateFilter);
  }, [stateFilter]);
  const hasGeoFilter = Boolean(stateFilter || districtFilter);
  const notesLen = form.notes.length;
  const selectedMapState = useMemo(
    () => (stateFilter.trim() ? mapStateKey(stateFilter.trim()) : null),
    [stateFilter]
  );
  const stateCountByCrm = useMemo(() => {
    const m = new Map<string, number>();
    for (const row of stateLeadStats) {
      m.set(row.state, row.lead_count);
      m.set(mapStateKey(row.state), row.lead_count);
    }
    return m;
  }, [stateLeadStats]);

  const mapStateRows = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of stateLeadStats) {
      const key = mapStateKey(row.state);
      counts.set(key, (counts.get(key) ?? 0) + row.lead_count);
    }
    return INDIA_STATE_PATHS.map((st) => ({
      state: st.name,
      lead_count: counts.get(st.name) ?? 0,
    }));
  }, [stateLeadStats]);

  const stateChipRows = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of stateLeadStats) {
      const key = mapStateKey(row.state);
      counts.set(key, (counts.get(key) ?? 0) + row.lead_count);
    }
    const tiles = buildLeadStateTileRows(counts);
    return tiles.sort((a, b) =>
      compareLeadStateTileOrder(
        { mapState: a.sortMapState, displayName: a.displayName },
        { mapState: b.sortMapState, displayName: b.displayName },
        new Map(
          tiles.map((t) => [
            t.sortMapState,
            dealerSalesTotalForTile(t.memberMapStates, dealerSalesByMapState),
          ])
        )
      )
    );
  }, [stateLeadStats, dealerSalesByMapState]);

  const mapOverview =
    geoOverviewOpen && !stateFilter.trim() && !languageFilter.trim();

  useEffect(() => {
    const app = document.querySelector(".app");
    if (!app) return undefined;
    if (mapOverview) app.classList.add("leads-map-mode");
    else app.classList.remove("leads-map-mode");
    return () => app.classList.remove("leads-map-mode");
  }, [mapOverview]);

  const languageCountByCode = useMemo(() => {
    const m = new Map<string, number>();
    for (const row of languageLeadStats) {
      m.set(row.language, row.lead_count);
    }
    return m;
  }, [languageLeadStats]);

  const focusedStateCount = useMemo(() => {
    if (!stateFilter.trim()) return null;
    if (/[|,]/.test(stateFilter)) {
      const tile = leadStateTileForFilter(stateFilter);
      if (tile) {
        let sum = 0;
        const counts = new Map<string, number>();
        for (const row of stateLeadStats) {
          counts.set(row.state, row.lead_count);
          counts.set(mapStateKey(row.state), row.lead_count);
        }
        for (const m of tile.memberMapStates) {
          sum += counts.get(m) ?? 0;
        }
        return sum;
      }
      return total;
    }
    return (
      stateCountByCrm.get(stateFilter) ??
      stateCountByCrm.get(mapStateKey(stateFilter)) ??
      total
    );
  }, [stateFilter, stateCountByCrm, stateLeadStats, total]);

  async function loadStateStats(status = statusFilter) {
    setStateStatsLoading(true);
    try {
      const params = new URLSearchParams();
      if (status && status !== "all") params.set("status", status);
      const q = params.toString();
      const data = await api<LeadByState>(`/api/leads/by-state${q ? `?${q}` : ""}`);
      setStateLeadStats(data.items);
      setStateLeadTotal(data.total);
    } catch (e) {
      console.error(e);
      setStateLeadStats([]);
      setStateLeadTotal(0);
    } finally {
      setStateStatsLoading(false);
    }
  }

  async function loadLanguageStats(status = statusFilter) {
    setLanguageStatsLoading(true);
    try {
      const params = new URLSearchParams();
      if (status && status !== "all") params.set("status", status);
      const q = params.toString();
      const data = await api<LeadByLanguage>(`/api/leads/by-language${q ? `?${q}` : ""}`);
      setLanguageLeadStats(data.items);
    } catch (e) {
      console.error(e);
      setLanguageLeadStats([]);
    } finally {
      setLanguageStatsLoading(false);
    }
  }

  async function load(opts: LoadOpts = {}) {
    const nextPage = opts.page ?? page;
    const q = opts.q ?? query;
    const status = opts.status ?? statusFilter;
    const state = opts.state ?? stateFilter;
    const district = opts.district ?? districtFilter;
    const language = opts.language ?? languageFilter;

    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        page: String(nextPage),
        page_size: String(PAGE_SIZE),
      });
      const term = q.trim();
      if (term) params.set("q", term);
      if (status && status !== "all") params.set("status", status);
      if (state.trim()) params.set("state", state.trim());
      if (district.trim()) params.set("district", district.trim());
      if (language.trim()) params.set("language", language.trim());
      const data = await api<LeadPage>(`/api/leads?${params}`);
      setRows(data.items);
      setTotal(data.total);
      setPage(data.page);
      setStatusCounts(data.status_counts || {});
      return data.items;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load leads");
      return null;
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setLoading(false);
    loadStateStats("all").catch(console.error);
    loadLanguageStats("all").catch(console.error);
    api<SalesByStateOut>("/api/dealers/sales-by-state")
      .then((data) => setDealerSalesByMapState(salesTotalsByMapState(data.items || [])))
      .catch(() => setDealerSalesByMapState(new Map()));
    api<LeadMeta>("/api/leads/meta")
      .then((m) => setMeta({ ...FALLBACK_META, ...m }))
      .catch(() => setMeta(FALLBACK_META));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!stateFilter.trim() && !languageFilter.trim()) return;
    window.dispatchEvent(new Event("crm:sidebar-open"));
    try {
      localStorage.setItem("crm_sidebar_open", "1");
    } catch {
      /* ignore */
    }
  }, [stateFilter, languageFilter]);

  const focusedLanguageCount = useMemo(() => {
    if (!languageFilter.trim()) return null;
    const iso = languageFilter.trim();
    const row = languageLeadStats.find(
      (r) => r.iso === iso || r.language === iso.split("-")[0]
    );
    return row?.lead_count ?? total;
  }, [languageFilter, languageLeadStats, total]);

  useEffect(() => {
    const prefill = (location.state as LeadsLocationState | null)?.createLead;
    if (!prefill) return;
    navigate(location.pathname + location.search, { replace: true, state: {} });
    setEditing(null);
    setCreating(true);
    setForm({
      ...emptyForm(),
      ...prefill,
      source: prefill.source?.trim() || "WhatsApp",
      status: (prefill.status || "new").toLowerCase(),
      priority: (prefill.priority || "medium").toLowerCase(),
      tags: prefill.tags ?? [],
    });
    setSaveError("");
    setPinHint("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.state]);

  useEffect(() => {
    const t = window.setTimeout(() => {
      const next = searchInput.trim();
      if (next === query) return;
      setQuery(next);
      load({ page: 1, q: next }).catch(console.error);
    }, 300);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput]);

  useEffect(() => {
    if (!editing && !creating) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape" && !saving) closeEdit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing, creating, saving]);

  function selectStatus(key: string) {
    if (key === statusFilter) return;
    setStatusFilter(key);
    load({ page: 1, status: key }).catch(console.error);
    loadStateStats(key).catch(console.error);
    loadLanguageStats(key).catch(console.error);
  }

  function crmStateFromMap(mapStateName: string): string {
    if (mapStateName === "Keralam") return "Keralam";
    return mapStateName;
  }

  function openLeadListSidebar() {
    window.dispatchEvent(new Event("crm:sidebar-open"));
    try {
      localStorage.setItem("crm_sidebar_open", "1");
    } catch {
      /* ignore */
    }
  }

  function openAllLeadsList() {
    setGeoOverviewOpen(false);
    setStateFilter("");
    setLanguageFilter("");
    setDistrictFilter("");
    setSearchInput("");
    setQuery("");
    setStatusFilter("all");
    openLeadListSidebar();
    load({ page: 1, q: "", status: "all", state: "", district: "", language: "" }).catch(
      console.error
    );
  }

  function selectStateFromMap(crmState: string) {
    if (!crmState.trim() || stateFilter === crmState) return;
    setGeoOverviewOpen(false);
    setStateFilter(crmState);
    setLanguageFilter("");
    setDistrictFilter("");
    setSearchInput("");
    setQuery("");
    setStatusFilter("all");
    load({ page: 1, q: "", status: "all", state: crmState, district: "", language: "" }).catch(
      console.error
    );
    loadStateStats("all").catch(console.error);
    loadLanguageStats("all").catch(console.error);
  }

  function selectLanguageFromTile(iso: string) {
    if (!iso.trim() || languageFilter === iso) return;
    setGeoOverviewOpen(false);
    setLanguageFilter(iso);
    setStateFilter("");
    setDistrictFilter("");
    setSearchInput("");
    setQuery("");
    setStatusFilter("all");
    load({ page: 1, q: "", status: "all", state: "", district: "", language: iso }).catch(
      console.error
    );
    loadLanguageStats("all").catch(console.error);
    loadStateStats("all").catch(console.error);
  }

  function backToMapOverview() {
    setGeoOverviewOpen(true);
    setStateFilter("");
    setLanguageFilter("");
    setDistrictFilter("");
    setSearchInput("");
    setQuery("");
    setStatusFilter("all");
    setRows([]);
    setTotal(0);
    setPage(1);
    setStatusCounts({});
    setError("");
    setLoading(false);
    loadStateStats("all").catch(console.error);
    loadLanguageStats("all").catch(console.error);
  }

  function onStateChange(value: string) {
    if (!value.trim()) {
      backToMapOverview();
      return;
    }
    setStateFilter(value);
    setDistrictFilter("");
    load({ page: 1, state: value, district: "" }).catch(console.error);
  }

  function onMapStateSelect(mapStateName: string) {
    selectStateFromMap(crmStateFromMap(mapStateName));
  }

  function onDistrictChange(value: string) {
    setDistrictFilter(value);
    load({ page: 1, district: value }).catch(console.error);
  }

  function clearFilters() {
    setSearchInput("");
    setQuery("");
    setStatusFilter("all");
    if (stateFilter) {
      load({ page: 1, q: "", status: "all", state: stateFilter, district: districtFilter }).catch(
        console.error
      );
      loadStateStats("all").catch(console.error);
    }
  }

  function openEditFromRowClick(e: MouseEvent, lead: Lead) {
    const el = e.target as HTMLElement;
    if (el.closest("input, button, a, select, label, .lead-check-col")) return;
    openEdit(lead);
  }

  function openEdit(lead: Lead) {
    setCreating(false);
    setEditing(lead);
    setForm(formFromLead(lead));
    setSaveError("");
    setPinHint("");
  }

  function openCreate() {
    setEditing(null);
    setCreating(true);
    setForm(emptyForm());
    setSaveError("");
    setPinHint("");
  }

  function closeEdit(force = false) {
    if (saving && !force) return;
    setEditing(null);
    setCreating(false);
    setForm(emptyForm());
    setSaveError("");
    setPinHint("");
    setPinLoading(false);
  }

  function setField<K extends keyof EditForm>(key: K, value: EditForm[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function lookupPin(raw: string) {
    const pin = raw.replace(/\D/g, "").slice(0, 6);
    if (pin.length !== 6) {
      setPinHint("");
      return;
    }
    setPinLoading(true);
    setPinHint("Looking up PIN…");
    try {
      const data = await api<PincodeLookup>(`/api/pincode/${pin}`);
      const state = data.state === "Kerala" ? "Keralam" : data.state || "";
      setForm((prev) => ({
        ...prev,
        pin,
        city: data.city || prev.city,
        district: data.district || prev.district,
        state: state || prev.state,
      }));
      const bits = [data.city, data.district, state || data.state].filter(Boolean);
      setPinHint(bits.length ? `Filled from PIN: ${bits.join(", ")}` : "PIN found");
    } catch (e) {
      setPinHint(e instanceof Error ? e.message : "PIN lookup failed");
    } finally {
      setPinLoading(false);
    }
  }

  function onPinChange(value: string) {
    const pin = value.replace(/\D/g, "").slice(0, 6);
    setField("pin", pin);
    setPinHint("");
    if (pin.length === 6) {
      lookupPin(pin).catch(console.error);
    }
  }

  function validateForm(): string | null {
    if (!form.name.trim()) return "Lead Name is required";
    if (!form.company.trim()) return "Company / Business Name is required";
    if (!form.phone.trim()) return "WhatsApp Number is required";
    if (form.notes.length > NOTES_MAX) return `Notes must be ≤ ${NOTES_MAX} characters`;
    const scoreRaw = form.lead_score.trim();
    if (scoreRaw) {
      const n = Number(scoreRaw);
      if (!Number.isInteger(n) || n < 0 || n > 100) {
        return "Lead score must be a whole number from 0 to 100";
      }
    }
    return null;
  }

  function buildPayload() {
    const estimatedRaw = form.estimated_value.replace(/,/g, "").trim();
    let estimated_value: number | null = null;
    if (estimatedRaw) {
      const n = Number(estimatedRaw);
      if (!Number.isFinite(n)) {
        throw new Error("Estimated Value must be a number");
      }
      estimated_value = n;
    }
    return {
      name: form.name.trim(),
      company: form.company.trim(),
      phone: form.phone.trim(),
      contact_person: form.contact_person.trim(),
      business_type: form.business_type.trim(),
      customer_website: form.customer_website.trim(),
      lead_score: (() => {
        const raw = form.lead_score.trim();
        if (!raw) return null;
        return Math.round(Number(raw));
      })(),
      source: form.source.trim(),
      owner_id: form.owner_id ? Number(form.owner_id) : null,
      status: form.status.trim().toLowerCase() || "new",
      priority: form.priority.trim().toLowerCase() || "medium",
      product_category: form.product_category.trim(),
      estimated_value,
      pin: form.pin.trim(),
      city: form.city.trim(),
      district: form.district.trim(),
      state: form.state.trim() === "Kerala" ? "Keralam" : form.state.trim(),
      address: form.address.trim(),
      next_follow_up: form.next_follow_up.trim() || null,
      tags: form.tags.join(", "),
      notes: form.notes.slice(0, NOTES_MAX),
    };
  }

  async function persistLead(): Promise<Lead | null> {
    if (!editing && !creating) return null;
    const err = validateForm();
    if (err) {
      setSaveError(err);
      return null;
    }
    setSaving(true);
    setSaveError("");
    try {
      const payload = buildPayload();
      if (creating) {
        const created = await api<Lead>("/api/leads", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        await load({ page: 1 });
        return created;
      }
      const updated = await api<Lead>(`/api/leads/${editing!.id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      setRows((prev) => prev.map((r) => (r.id === updated.id ? { ...r, ...updated } : r)));
      return updated;
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "Save failed");
      return null;
    } finally {
      setSaving(false);
    }
  }

  async function saveEdit(e: FormEvent) {
    e.preventDefault();
    const updated = await persistLead();
    if (updated) closeEdit(true);
  }

  async function saveAndNext() {
    const updated = await persistLead();
    if (!updated || !editing) return;
    const idx = rows.findIndex((r) => r.id === editing.id);
    const next = idx >= 0 ? rows[idx + 1] : undefined;
    if (next) {
      openEdit(next);
    } else {
      closeEdit(true);
    }
  }

  const from = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const to = Math.min(page * PAGE_SIZE, total);
  const allCount = Object.values(statusCounts).reduce((a, b) => a + b, 0);
  const filtersActive = Boolean(query || statusFilter !== "all" || hasGeoFilter || languageFilter);

  const businessTypes = useMemo(() => {
    const set = new Set(meta.business_types);
    if (form.business_type) set.add(form.business_type);
    return Array.from(set);
  }, [meta.business_types, form.business_type]);

  const sources = useMemo(() => {
    const set = new Set(meta.sources);
    if (form.source) set.add(form.source);
    return Array.from(set);
  }, [meta.sources, form.source]);

  const products = useMemo(() => {
    const set = new Set(meta.product_categories);
    if (form.product_category) set.add(form.product_category);
    return Array.from(set);
  }, [meta.product_categories, form.product_category]);

  return (
    <>
      {error ? <p style={{ color: "#b91c1c", marginBottom: 12 }}>{error}</p> : null}

      {mapOverview ? (
        <div className="leads-map-page">
        <section className="panel lead-geo-panel lead-map-overview">
          <div className="panelhead lead-map-overview-head">
            <div className="lead-map-overview-title">
              <span className="lead-map-overview-icon" aria-hidden>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <rect x="3" y="12" width="4" height="9" rx="1" fill="currentColor" opacity="0.9" />
                  <rect x="10" y="7" width="4" height="14" rx="1" fill="currentColor" />
                  <rect x="17" y="3" width="4" height="18" rx="1" fill="currentColor" opacity="0.75" />
                </svg>
              </span>
              <div>
                <h2>Leads by State &amp; Language</h2>
                <p>
                  {stateStatsLoading || languageStatsLoading
                    ? "Loading totals…"
                    : `${stateLeadTotal.toLocaleString("en-IN")} leads · language tiles use state market totals · click a tile or state`}
                </p>
              </div>
            </div>
            <button type="button" className="btn primary lead-new-btn" onClick={openCreate}>
              + New Lead
            </button>
          </div>
          <div className="lead-state-wrap lead-state-wrap--overview">
            <IndiaSalesMap
              items={mapStateRows}
              mode="leads"
              layout="hero"
              selectedMapState={selectedMapState}
              highlightMapStates={hoveredTileMembers ?? undefined}
              onSelectState={onMapStateSelect}
            />
            <div className="lead-overview-side">
              <button
                type="button"
                className="lead-all-leads-tile"
                onClick={openAllLeadsList}
                title={`All leads · ${stateLeadTotal.toLocaleString("en-IN")} total`}
              >
                <span className="lead-all-leads-icon" aria-hidden>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <rect x="3" y="5" width="18" height="14" rx="2" stroke="currentColor" strokeWidth="1.8" />
                    <path d="M7 9h10M7 13h6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                  </svg>
                </span>
                <span className="lead-all-leads-body">
                  <span className="lead-all-leads-label">All leads</span>
                  <span className="lead-all-leads-hint">Open full list · clear filters</span>
                </span>
                <span className="lead-all-leads-count">
                  {stateStatsLoading ? "…" : stateLeadTotal.toLocaleString("en-IN")}
                </span>
              </button>
              <div className="lead-lang-section">
                <div className="lead-lang-grid" role="list" aria-label="Lead count by business language">
                  {LEAD_LANGUAGE_MARKETS.map((style) => {
                    const fromApi = languageLeadStats.find((r) => r.language === style.code);
                    const row: LeadLanguageRow = fromApi ?? {
                      language: style.code,
                      iso: style.iso,
                      label: style.label,
                      lead_count: languageCountByCode.get(style.code) ?? 0,
                    };
                    return { row, style };
                  }).map(({ row, style }) => (
                    <button
                      key={row.iso}
                      type="button"
                      role="listitem"
                      className="lead-lang-tile"
                      style={
                        {
                          "--lead-lang-accent": style.accent,
                          "--lead-lang-soft": style.accentSoft,
                        } as CSSProperties
                      }
                      onClick={() => selectLanguageFromTile(row.iso)}
                      title={`${row.label} · ${row.lead_count} leads`}
                    >
                      <span className="lead-lang-glyph" aria-hidden>
                        {style.glyph}
                      </span>
                      <span className="lead-lang-body">
                        <span className="lead-lang-label">{row.label}</span>
                        <span className="lead-lang-count">
                          {languageStatsLoading ? "…" : row.lead_count.toLocaleString("en-IN")}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
              <div className="lead-state-section" aria-label="States by lead count">
                <div className="lead-state-chip-grid" role="list">
                  {stateChipRows.map((row) => {
                    const langStyle = languageMarketStyleForBlock(marketLanguageBlockForTile(row));
                    return (
                      <button
                        key={row.tileId}
                        type="button"
                        role="listitem"
                        className="lead-state-chip"
                        style={
                          {
                            "--lead-lang-accent": langStyle.accent,
                            "--lead-lang-soft": langStyle.accentSoft,
                          } as CSSProperties
                        }
                        onClick={() => selectStateFromMap(row.filterState)}
                        onMouseEnter={() => setHoveredTileMembers(row.memberMapStates)}
                        onMouseLeave={() => setHoveredTileMembers(null)}
                        title={`${row.displayName} · ${row.lead_count} leads`}
                      >
                        <span className="lead-state-chip-name">{row.displayName}</span>
                        <span className="lead-state-chip-count">
                          {stateStatsLoading ? "…" : row.lead_count.toLocaleString("en-IN")}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
        </section>
        </div>
      ) : (
        <>
          <div className="lead-state-focus lead-state-focus--bar" aria-live="polite">
            <button type="button" className="lead-back-map" onClick={backToMapOverview}>
              ← Back to map
            </button>
            <span>
              {languageFilter.trim() ? (
                <>
                  <strong>{leadLanguageFilterDisplayName(languageFilter)}</strong>
                  {focusedLanguageCount != null ? (
                    <>
                      {" "}
                      · <strong>{focusedLanguageCount.toLocaleString("en-IN")}</strong> leads
                      {focusedLanguageCount !== total
                        ? ` (${total.toLocaleString("en-IN")} in this filter)`
                        : ""}
                    </>
                  ) : null}
                </>
              ) : stateFilter.trim() ? (
                <>
                  <strong>{leadStateFilterDisplayName(stateFilter)}</strong>
                  {focusedStateCount != null ? (
                    <>
                      {" "}
                      · <strong>{focusedStateCount.toLocaleString("en-IN")}</strong> leads
                      {focusedStateCount !== total
                        ? ` (${total.toLocaleString("en-IN")} in this filter)`
                        : ""}
                    </>
                  ) : null}
                </>
              ) : (
                <>
                  <strong>All leads</strong>
                  {" "}
                  · <strong>{total.toLocaleString("en-IN")}</strong> in list
                </>
              )}
            </span>
            <button type="button" className="lead-clear-state" onClick={backToMapOverview}>
              {languageFilter.trim()
                ? "Clear language"
                : stateFilter.trim()
                  ? "Clear state"
                  : "Back to map"}
            </button>
          </div>

          <div className="lead-status-tiles">
        {STATUS_TILES.map((t) => {
          const count = t.key === "all" ? allCount : statusCounts[t.key] ?? 0;
          const active = statusFilter === t.key;
          return (
            <button
              key={t.key}
              type="button"
              className={`lead-status-tile${active ? " active" : ""}`}
              onClick={() => selectStatus(t.key)}
            >
              <span className="lead-status-label">{t.label}</span>
              <span className="lead-status-count">{count}</span>
            </button>
          );
        })}
      </div>

      <div className="panel">
        <div className="lead-toolbar">
          <button type="button" className="btn primary lead-new-btn" onClick={openCreate}>
            + New Lead
          </button>
          <input
            className="lead-search"
            placeholder="Search name, phone, city, district, state…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            aria-label="Search leads"
          />
          <select
            className="lead-select"
            value={stateFilter}
            onChange={(e) => onStateChange(e.target.value)}
            aria-label="Filter by state"
          >
            <option value="">All states</option>
            {INDIA_STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            className="lead-select"
            value={districtFilter}
            onChange={(e) => onDistrictChange(e.target.value)}
            aria-label="Filter by district"
            disabled={!stateFilter}
          >
            <option value="">{stateFilter ? "All districts" : "Select state first"}</option>
            {districtOptions.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <button type="button" className="lead-clear" disabled={!filtersActive && !loading} onClick={clearFilters}>
            Clear
          </button>
          <strong className="lead-range">{loading ? "Loading…" : `${from}–${to} of ${total}`}</strong>
          <div className="lead-pager">
            <button type="button" disabled={loading || page <= 1} onClick={() => load({ page: page - 1 })}>
              Prev
            </button>
            <span>
              Page {page} / {pageCount}
            </span>
            <button type="button" disabled={loading || page >= pageCount} onClick={() => load({ page: page + 1 })}>
              Next
            </button>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Phone</th>
              <th>City</th>
              <th>District</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.id}
                className="lead-row"
                title="Click to edit"
                onClick={(e) => openEditFromRowClick(e, r)}
              >
                <td>{r.name || r.company || "—"}</td>
                <td>{r.phone || "—"}</td>
                <td>{r.city || "—"}</td>
                <td>{r.district || "—"}</td>
                <td>{r.state || "—"}</td>
              </tr>
            ))}
            {!loading && rows.length === 0 ? (
              <tr>
                <td colSpan={5} style={{ color: "#64748b" }}>
                  {filtersActive ? "No leads match your filters." : "No leads yet."}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
        </>
      )}

      {editing || creating ? (
        <div
          className="emp-overlay emp-overlay--sheet lead-edit-overlay"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeEdit();
          }}
        >
          <section
            className="emp-drawer lead-edit-modal lead-edit-modal--full"
            role="dialog"
            aria-modal="true"
            aria-labelledby="lead-edit-title"
          >
            <div className="lead-edit-head">
              <div className="lead-edit-head-text">
                <div className="lead-edit-title-row">
                  <span className="lead-edit-title-icon" aria-hidden>
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                      <path
                        d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z"
                        stroke="currentColor"
                        strokeWidth="1.8"
                      />
                      <path
                        d="M4 20c1.6-3.2 4.4-4.8 8-4.8s6.4 1.6 8 4.8"
                        stroke="currentColor"
                        strokeWidth="1.8"
                        strokeLinecap="round"
                      />
                    </svg>
                  </span>
                  <h2 id="lead-edit-title">{creating ? "New Lead" : "Edit Lead"}</h2>
                </div>
                <p>
                  {creating
                    ? "Add a new lead with contact, qualification, and location details."
                    : "Update lead details, qualification, and location information."}
                </p>
              </div>
              <button
                type="button"
                className="iconbtn lead-edit-close"
                onClick={() => closeEdit()}
                aria-label="Close"
                disabled={saving}
              >
                ✕
              </button>
            </div>

            <form className="emp-form" onSubmit={saveEdit}>
              <div className="emp-formbody lead-edit-body">
                <div className="lead-edit-cards">
                {/* 1. Basic Information */}
                <section className="lead-edit-section lead-edit-section--card">
                  <header className="lead-edit-section-head">
                    <span className="lead-edit-section-icon" aria-hidden>
                      ⓘ
                    </span>
                    <h3>Basic Information</h3>
                  </header>
                  <div className="emp-fields lead-edit-fields lead-edit-fields--card">
                    <div className="field">
                      <label htmlFor="lead-edit-name">
                        Lead Name <span className="req">*</span>
                      </label>
                      <input
                        id="lead-edit-name"
                        value={form.name}
                        onChange={(e) => setField("name", e.target.value)}
                        autoFocus
                        required
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-company">
                        Company / Business Name <span className="req">*</span>
                      </label>
                      <input
                        id="lead-edit-company"
                        value={form.company}
                        onChange={(e) => setField("company", e.target.value)}
                        required
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-phone">
                        WhatsApp Number <span className="req">*</span>
                      </label>
                      <input
                        id="lead-edit-phone"
                        value={form.phone}
                        onChange={(e) => setField("phone", e.target.value)}
                        inputMode="tel"
                        autoComplete="tel"
                        required
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-contact">Contact Person</label>
                      <input
                        id="lead-edit-contact"
                        value={form.contact_person}
                        onChange={(e) => setField("contact_person", e.target.value)}
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-biz">Business Type</label>
                      <select
                        id="lead-edit-biz"
                        value={form.business_type}
                        onChange={(e) => setField("business_type", e.target.value)}
                      >
                        <option value="">Select business type</option>
                        {businessTypes.map((b) => (
                          <option key={b} value={b}>
                            {b}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-website">Customer Website</label>
                      <input
                        id="lead-edit-website"
                        type="url"
                        value={form.customer_website}
                        onChange={(e) => setField("customer_website", e.target.value)}
                        placeholder="https://…"
                        inputMode="url"
                        autoComplete="url"
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-score">Lead score</label>
                      <input
                        id="lead-edit-score"
                        type="number"
                        min={0}
                        max={100}
                        step={1}
                        value={form.lead_score}
                        onChange={(e) =>
                          setField("lead_score", e.target.value.replace(/[^\d]/g, "").slice(0, 3))
                        }
                        inputMode="numeric"
                        placeholder="0–100"
                        aria-describedby="lead-edit-score-hint"
                      />
                      <p id="lead-edit-score-hint" className="lead-pin-hint">
                        Optional quality score from 0 (cold) to 100 (hot).
                      </p>
                    </div>
                  </div>
                </section>

                {/* 2. Lead Qualification */}
                <section className="lead-edit-section lead-edit-section--card">
                  <header className="lead-edit-section-head">
                    <span className="lead-edit-section-icon" aria-hidden>
                      ⌖
                    </span>
                    <h3>Lead Qualification</h3>
                  </header>
                  <div className="emp-fields lead-edit-fields lead-edit-fields--card">
                    <div className="field">
                      <label htmlFor="lead-edit-source">Lead Source</label>
                      <select
                        id="lead-edit-source"
                        value={form.source}
                        onChange={(e) => setField("source", e.target.value)}
                      >
                        <option value="">Select source</option>
                        {sources.map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-owner">Lead Owner</label>
                      <select
                        id="lead-edit-owner"
                        value={form.owner_id}
                        onChange={(e) => setField("owner_id", e.target.value)}
                      >
                        <option value="">Select owner</option>
                        {meta.owners.map((o) => (
                          <option key={o.id} value={String(o.id)}>
                            {o.name}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-status">Lead Stage / Status</label>
                      <select
                        id="lead-edit-status"
                        value={form.status}
                        onChange={(e) => setField("status", e.target.value)}
                      >
                        {meta.statuses.map((s) => (
                          <option key={s} value={s}>
                            {titleCaseStatus(s)}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-priority">Priority</label>
                      <div className="lead-priority-wrap">
                        <span
                          className="lead-priority-dot"
                          style={{ background: priorityDot(form.priority) }}
                          aria-hidden
                        />
                        <select
                          id="lead-edit-priority"
                          value={form.priority}
                          onChange={(e) => setField("priority", e.target.value)}
                        >
                          {meta.priorities.map((p) => (
                            <option key={p} value={p}>
                              {titleCaseStatus(p)}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <div className="field span-2">
                      <label htmlFor="lead-edit-product">
                        Interested Product / Product Category
                      </label>
                      <select
                        id="lead-edit-product"
                        value={form.product_category}
                        onChange={(e) => setField("product_category", e.target.value)}
                      >
                        <option value="">Select product category</option>
                        {products.map((p) => (
                          <option key={p} value={p}>
                            {p}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-value">Estimated Value / Potential</label>
                      <div className="lead-rupee-wrap">
                        <span className="lead-rupee" aria-hidden>
                          ₹
                        </span>
                        <input
                          id="lead-edit-value"
                          value={form.estimated_value}
                          onChange={(e) =>
                            setField("estimated_value", e.target.value.replace(/[^\d.]/g, ""))
                          }
                          inputMode="decimal"
                          placeholder="0"
                        />
                      </div>
                    </div>
                  </div>
                </section>

                {/* 3. Location */}
                <section className="lead-edit-section lead-edit-section--card">
                  <header className="lead-edit-section-head">
                    <span className="lead-edit-section-icon" aria-hidden>
                      📍
                    </span>
                    <h3>Location</h3>
                  </header>
                  <div className="emp-fields lead-edit-fields lead-edit-fields--card">
                    <div className="field">
                      <label htmlFor="lead-edit-pin">PIN</label>
                      <input
                        id="lead-edit-pin"
                        value={form.pin}
                        onChange={(e) => onPinChange(e.target.value)}
                        onBlur={() => {
                          if (form.pin.replace(/\D/g, "").length === 6) {
                            lookupPin(form.pin).catch(console.error);
                          }
                        }}
                        inputMode="numeric"
                        maxLength={6}
                        placeholder="6-digit PIN"
                        aria-describedby="lead-edit-pin-hint"
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-city">City</label>
                      <input
                        id="lead-edit-city"
                        value={form.city}
                        onChange={(e) => setField("city", e.target.value)}
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-district">District</label>
                      <input
                        id="lead-edit-district"
                        value={form.district}
                        onChange={(e) => setField("district", e.target.value)}
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="lead-edit-state">State</label>
                      <select
                        id="lead-edit-state"
                        value={form.state}
                        onChange={(e) => setField("state", e.target.value)}
                      >
                        <option value="">Select state</option>
                        {INDIA_STATES.map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                        {form.state && !INDIA_STATES.includes(form.state) ? (
                          <option value={form.state}>{form.state}</option>
                        ) : null}
                      </select>
                    </div>
                    <div className="field full">
                      <label htmlFor="lead-edit-address">Address</label>
                      <textarea
                        id="lead-edit-address"
                        value={form.address}
                        onChange={(e) => setField("address", e.target.value)}
                        rows={2}
                        placeholder="Street, landmark…"
                      />
                    </div>
                  </div>
                  <p id="lead-edit-pin-hint" className={`lead-pin-hint${pinLoading ? " loading" : ""}`}>
                    {pinHint || "City, district, and state can be filled manually or from PIN."}
                  </p>
                </section>

                {/* 4. Notes */}
                <section className="lead-edit-section lead-edit-section--notes">
                  <header className="lead-edit-section-head">
                    <span className="lead-edit-section-icon" aria-hidden>
                      📝
                    </span>
                    <h3>Notes</h3>
                  </header>
                  <div className="emp-fields lead-edit-fields">
                    <div className="field full">
                      <label htmlFor="lead-edit-notes">Notes</label>
                      <textarea
                        id="lead-edit-notes"
                        className="lead-notes"
                        value={form.notes}
                        onChange={(e) => setField("notes", e.target.value.slice(0, NOTES_MAX))}
                        rows={3}
                        maxLength={NOTES_MAX}
                        placeholder="Call notes, requirements, objections…"
                      />
                      <div className="lead-notes-count">
                        {notesLen} / {NOTES_MAX}
                      </div>
                    </div>
                  </div>
                </section>
                </div>

                {saveError ? <p className="lead-edit-error">{saveError}</p> : null}
              </div>

              <div className="emp-formfoot lead-edit-foot">
                {!creating ? (
                  <small className="lead-edit-footnote">Click any row to open this editor.</small>
                ) : (
                  <span />
                )}
                <div className="actions">
                  <button type="button" className="btn" onClick={() => closeEdit()} disabled={saving}>
                    Cancel
                  </button>
                  <button type="submit" className="btn primary" disabled={saving || pinLoading}>
                    {saving ? "Saving…" : creating ? "Create Lead" : "Save"}
                  </button>
                  {!creating ? (
                    <button
                      type="button"
                      className="btn primary lead-save-next"
                      disabled={saving || pinLoading}
                      onClick={() => saveAndNext().catch(console.error)}
                    >
                      {saving ? "Saving…" : "Save & Next →"}
                    </button>
                  ) : null}
                </div>
              </div>
            </form>
          </section>
        </div>
      ) : null}
    </>
  );
}
