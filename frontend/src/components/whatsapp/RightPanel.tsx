import { useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { formatLanguageLabel } from "../../lib/languageDisplay";
import AiReplyAssistant from "./AiReplyAssistant";
import AiSummaryCard, { type AiSummary } from "./AiSummaryCard";
import { formatInr } from "../BusinessReportCard";
import {
  CalendarIcon,
  CheckCircleIcon,
  FileTextIcon,
  LinkIcon,
  MoreVerticalIcon,
  PlusIcon,
  UserCogIcon,
  UserPlusIcon,
} from "./icons";

export type DealerBrief = {
  id: number;
  name: string;
  company?: string;
  phone: string;
  code?: string;
  city?: string;
  state?: string;
  total_sales?: number;
};

export type OnboardingBrief = {
  step?: string;
  lead_id?: number | null;
  at?: string | null;
  ran?: boolean;
};

export type ConvBrief = {
  id: string;
  display_name: string;
  customer_name: string;
  customer_phone: string;
  customer_photo?: string;
  initial: string;
  color: string;
  status: string;
  assigned_agent: string;
  account: string;
  language?: string;
  region?: string;
  customer_type?: string;
  dealer_id?: number | null;
  dealer?: DealerBrief | null;
  onboarding_step?: string;
  onboarding_lead_id?: number | null;
  onboarding?: OnboardingBrief | null;
};

export type MsgBrief = {
  id: string;
  direction: "in" | "out" | "note";
  body: string;
  sender_name: string;
  created_at?: string;
  is_voice?: boolean;
  transcript?: string;
};

type Agent = { id: number; name: string; open_conversations?: number; open_count?: number };

type RelatedPayload = {
  dealer: DealerBrief | null;
  leads: { id: number; name: string; phone: string; status: string; company?: string }[];
  open_tasks: { id: number; title: string; status: string; priority: string; due_date?: string | null }[];
  quotations: unknown[];
  sales_orders: unknown[];
  previous_purchases: unknown[];
  service_tickets: unknown[];
  subscription_renewals: unknown[];
  unavailable?: string[];
};

type TabId = "Customer" | "Activities" | "Notes" | "Related";

type Props = {
  conv: ConvBrief | null;
  messages: MsgBrief[];
  summary: AiSummary | null;
  outlineLoading: boolean;
  outlineError: string;
  outlineAt: string | null;
  outlineStale?: boolean;
  onRegenerate: () => void;
  WaDp: ComponentType<{
    photo?: string;
    initial: string;
    color?: string;
    size?: number;
    className?: string;
    onPhotoClick?: () => void;
  }>;
  onViewContactPhoto?: (photo: string, name?: string) => void;
  dealerSearchOpen: boolean;
  setDealerSearchOpen: (v: boolean) => void;
  dealerQ: string;
  setDealerQ: (v: string) => void;
  dealerHits: DealerBrief[];
  dealerSearching: boolean;
  selectedDealerId: number | null;
  setSelectedDealerId: (v: number | null) => void;
  linkingDealer: boolean;
  delinkingDealer: boolean;
  openLinkDealerPanel: () => void;
  linkSelectedDealer: () => Promise<void>;
  delinkDealer: () => Promise<void>;
  onEditContact: () => void;
  onMarkSpam?: () => void;
  onToast: (msg: string) => void;
  onRefreshConv: () => Promise<void>;
  onUseAiReply?: (text: string) => void;
  fmtTime: (v?: string | null) => string;
};

function insightFromSummary(summary: AiSummary | null): { title: string; detail: string }[] {
  if (!summary) return [];
  const items: { title: string; detail: string }[] = [];
  if (summary.current_issue) items.push({ title: "Issue identified", detail: summary.current_issue });
  if (summary.customer_intent) items.push({ title: "Customer intent", detail: summary.customer_intent });
  if (summary.recommended_action) items.push({ title: "Recommended action", detail: summary.recommended_action });
  if (summary.product_interest?.length) {
    items.push({ title: "Product identified", detail: summary.product_interest.join(", ") });
  }
  return items.slice(0, 4);
}

export default function RightPanel(props: Props) {
  const {
    conv,
    messages,
    summary,
    outlineLoading,
    outlineError,
    outlineAt,
    outlineStale,
    onRegenerate,
    WaDp,
    onViewContactPhoto,
    dealerSearchOpen,
    setDealerSearchOpen,
    dealerQ,
    setDealerQ,
    dealerHits,
    dealerSearching,
    selectedDealerId,
    setSelectedDealerId,
    linkingDealer,
    delinkingDealer,
    openLinkDealerPanel,
    linkSelectedDealer,
    delinkDealer,
    onEditContact,
    onMarkSpam,
    onToast,
    onRefreshConv,
    onUseAiReply,
    fmtTime,
  } = props;
  const navigate = useNavigate();

  const [tab, setTab] = useState<TabId>("Customer");
  const [menuOpen, setMenuOpen] = useState(false);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [assignOpen, setAssignOpen] = useState(false);
  const [related, setRelated] = useState<RelatedPayload | null>(null);
  const [relatedLoading, setRelatedLoading] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  const dealerSearchRef = useRef<HTMLInputElement | null>(null);
  const [noteSaving, setNoteSaving] = useState(false);
  const [taskBusy, setTaskBusy] = useState(false);
  const [leadBusy, setLeadBusy] = useState(false);
  const [activityFilter, setActivityFilter] = useState<"all" | "in" | "out" | "note" | "ai">("all");

  useEffect(() => {
    setTab("Customer");
    setMenuOpen(false);
    setAssignOpen(false);
    setRelated(null);
    setNoteDraft("");
  }, [conv?.id]);

  useEffect(() => {
    if (!conv || (tab !== "Related" && tab !== "Customer")) return;
    let cancelled = false;
    setRelatedLoading(true);
    api<RelatedPayload>(`/api/whatsapp/conversations/${conv.id}/related`)
      .then((r) => {
        if (!cancelled) setRelated(r);
      })
      .catch(() => {
        if (!cancelled) setRelated(null);
      })
      .finally(() => {
        if (!cancelled) setRelatedLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conv?.id, tab]);

  const notes = useMemo(
    () => messages.filter((m) => m.direction === "note").slice().reverse(),
    [messages],
  );

  const activities = useMemo(() => {
    const rows = messages
      .slice()
      .reverse()
      .map((m) => {
        const kind =
          m.direction === "note"
            ? "Internal note"
            : m.direction === "out"
              ? "Employee replied"
              : m.is_voice || m.transcript
                ? "Voice message received"
                : "Customer message";
        return {
          id: m.id,
          kind,
          filter: m.direction as "in" | "out" | "note",
          detail: (m.transcript || m.body || "").slice(0, 140) || "—",
          who: m.sender_name || (m.direction === "in" ? "Customer" : "Agent"),
          at: m.created_at,
        };
      });
    const ai = insightFromSummary(summary).map((x, i) => ({
      id: `ai-${i}`,
      kind: x.title,
      filter: "ai" as const,
      detail: x.detail,
      who: "AI",
      at: outlineAt || summary?.updated_at || undefined,
    }));
    return [...ai, ...rows];
  }, [messages, summary, outlineAt]);

  const filteredActivities = useMemo(() => {
    if (activityFilter === "all") return activities;
    return activities.filter((a) => a.filter === activityFilter);
  }, [activities, activityFilter]);

  const profileCityState = useMemo(() => {
    if (!conv) return "";
    const parts = [conv.dealer?.city, conv.dealer?.state]
      .map((x) => (x || "").trim())
      .filter(Boolean);
    if (parts.length) return parts.join(", ");
    return (conv.region || "").trim();
  }, [conv?.dealer?.city, conv?.dealer?.state, conv?.region]);

  const profileLanguage = useMemo(() => {
    const label = formatLanguageLabel(conv?.language);
    return label || "—";
  }, [conv?.language]);

  async function createTask() {
    if (!conv) return;
    setTaskBusy(true);
    try {
      const title =
        summary?.topic?.trim() ||
        `Follow up: ${conv.display_name || conv.customer_phone}`;
      const description = [
        summary?.recommended_action ? `Next action: ${summary.recommended_action}` : "",
        summary?.current_issue ? `Issue: ${summary.current_issue}` : "",
        summary?.customer_intent ? `Intent: ${summary.customer_intent}` : "",
        `WhatsApp: ${conv.customer_phone}`,
        `Chat: ${conv.id}`,
      ]
        .filter(Boolean)
        .join("\n");
      await api("/api/work", {
        method: "POST",
        body: JSON.stringify({
          title: title.slice(0, 200),
          description,
          status: "todo",
          priority: (summary?.followup_priority || "medium").toLowerCase() === "high" ? "high" : "medium",
          due_date: summary?.suggested_followup_at || null,
        }),
      });
      onToast("Task created");
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Task failed");
    } finally {
      setTaskBusy(false);
    }
  }

  async function createLead() {
    if (!conv) return;
    setLeadBusy(true);
    try {
      await api("/api/leads", {
        method: "POST",
        body: JSON.stringify({
          name: conv.customer_name || conv.display_name || "",
          phone: conv.customer_phone,
          city: conv.region || "",
          source: "whatsapp",
          status: "new",
          notes: [
            summary?.topic ? `Topic: ${summary.topic}` : "",
            summary?.customer_intent ? `Intent: ${summary.customer_intent}` : "",
            summary?.recommended_action ? `Action: ${summary.recommended_action}` : "",
            summary?.product_interest?.length ? `Products: ${summary.product_interest.join(", ")}` : "",
          ]
            .filter(Boolean)
            .join("\n"),
        }),
      });
      onToast("Lead created");
      setTab("Related");
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Lead failed");
    } finally {
      setLeadBusy(false);
    }
  }

  async function scheduleFollowUp() {
    if (!conv) return;
    setTaskBusy(true);
    try {
      const due =
        summary?.suggested_followup_at ||
        new Date(Date.now() + 24 * 3600 * 1000).toISOString().slice(0, 10);
      await api("/api/work", {
        method: "POST",
        body: JSON.stringify({
          title: `Follow-up: ${conv.display_name || conv.customer_phone}`.slice(0, 200),
          description: summary?.recommended_action || "Scheduled from WhatsApp inbox",
          status: "todo",
          priority: (summary?.followup_priority || "medium").toLowerCase() === "high" ? "high" : "medium",
          due_date: due,
        }),
      });
      onToast(`Follow-up scheduled for ${due}`);
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Follow-up failed");
    } finally {
      setTaskBusy(false);
    }
  }

  async function loadAgents() {
    try {
      const r = await api<{ items: Agent[] }>("/api/whatsapp/agents");
      setAgents(r.items || []);
    } catch {
      setAgents([]);
    }
  }

  async function assignOwner(employeeId: number | null, assignToMe = false) {
    if (!conv) return;
    try {
      await api(`/api/whatsapp/conversations/${conv.id}/assign`, {
        method: "POST",
        body: JSON.stringify(assignToMe ? { assign_to_me: true } : { employee_id: employeeId }),
      });
      await onRefreshConv();
      setAssignOpen(false);
      onToast(assignToMe ? "Assigned to you" : employeeId ? "Owner assigned" : "Owner cleared");
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Assign failed");
    }
  }

  async function saveNote() {
    if (!conv || !noteDraft.trim()) return;
    setNoteSaving(true);
    try {
      await api(`/api/whatsapp/conversations/${conv.id}/notes`, {
        method: "POST",
        body: JSON.stringify({ body: noteDraft.trim() }),
      });
      setNoteDraft("");
      onToast("Note saved");
      await onRefreshConv();
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Note failed");
    } finally {
      setNoteSaving(false);
    }
  }

  useEffect(() => {
    if (!dealerSearchOpen) return;
    const t = window.setTimeout(() => dealerSearchRef.current?.focus(), 40);
    return () => window.clearTimeout(t);
  }, [dealerSearchOpen]);

  function closeLinkDealer() {
    setDealerSearchOpen(false);
    setSelectedDealerId(null);
  }

  function openLinkDealer() {
    if (!conv) return;
    setTab("Customer");
    openLinkDealerPanel();
  }

  return (
    <aside className="wa-profile">
      <div className="wa-rtabs" role="tablist">
        {(["Customer", "Activities", "Notes", "Related"] as TabId[]).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            className={tab === t ? "on" : ""}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="wa-rscroll">
        {!conv ? (
          <div className="wa-empty">No conversation selected</div>
        ) : tab === "Customer" ? (
          <>
            <section className="wa-card wa-profile-card">
              <WaDp
                photo={conv.customer_photo}
                initial={conv.initial}
                color={conv.color}
                size={40}
                onPhotoClick={
                  conv.customer_photo
                    ? () => onViewContactPhoto?.(conv.customer_photo!, conv.display_name || conv.customer_phone)
                    : undefined
                }
              />
              <div className="wa-profile-main">
                <div className="wa-profile-top">
                  <b>{conv.display_name || conv.customer_phone}</b>
                  <span className={`wa-tag ${conv.dealer ? "dealer" : "new"}`}>
                    {conv.dealer ? "Dealer" : conv.customer_type || "Customer"}
                  </span>
                  {(conv.onboarding?.ran || conv.onboarding_step) && (
                    <span
                      className="wa-tag new"
                      title={
                        conv.onboarding?.lead_id
                          ? `Lead #${conv.onboarding.lead_id}`
                          : conv.onboarding_step || conv.onboarding?.step || "ran"
                      }
                    >
                      Auto:{" "}
                      {(conv.onboarding?.step || conv.onboarding_step || "ran").replace(/_/g, " ")}
                    </span>
                  )}
                </div>
                {conv.dealer && conv.dealer.total_sales != null ? (
                  <p className="wa-dealer-sales-total">
                    Sales{" "}
                    <span className="wa-dealer-sales-amount">
                      {formatInr(conv.dealer.total_sales)}
                    </span>
                  </p>
                ) : null}
                {profileCityState ? (
                  <p className="wa-profile-place">{profileCityState}</p>
                ) : null}
                <p>{conv.customer_phone}</p>
                <div className="wa-profile-grid">
                  <span>Account</span>
                  <span>{conv.account || "—"}</span>
                  <span>Location</span>
                  <span>{profileCityState || "—"}</span>
                  <span>Language</span>
                  <span>{profileLanguage}</span>
                  <span>Dealer</span>
                  <span className={conv.dealer ? "wa-dealer-linked-cell" : undefined}>
                    {conv.dealer ? (
                      <span className="wa-dealer-linked-row">
                        <Link
                          className="wa-inline-link"
                          to={`/dealers?q=${encodeURIComponent(conv.dealer.phone || conv.dealer.name)}`}
                          title={conv.dealer.name || conv.dealer.company}
                        >
                          {conv.dealer.name || conv.dealer.company || "View dealer"}
                        </Link>
                        <button
                          type="button"
                          className="wa-text-btn wa-delink-btn"
                          disabled={delinkingDealer}
                          onClick={() => delinkDealer().catch(() => null)}
                        >
                          {delinkingDealer ? "Delinking…" : "Delink"}
                        </button>
                      </span>
                    ) : (
                      <button type="button" className="wa-text-btn" onClick={openLinkDealer}>
                        Link Dealer
                      </button>
                    )}
                  </span>
                </div>
              </div>
              <div className="wa-profile-menu">
                <button type="button" className="wa-icon-sm" aria-label="More" onClick={() => setMenuOpen((v) => !v)}>
                  <MoreVerticalIcon />
                </button>
                {menuOpen ? (
                  <div className="wa-menu-pop">
                    <button type="button" onClick={() => { setMenuOpen(false); onEditContact(); }}>Edit contact</button>
                    <button type="button" onClick={() => { setMenuOpen(false); openLinkDealer(); }}>
                      {conv.dealer ? "Change dealer" : "Link dealer"}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setMenuOpen(false);
                        setAssignOpen(true);
                        loadAgents().catch(() => null);
                      }}
                    >
                      Assign owner
                    </button>
                  </div>
                ) : null}
              </div>
            </section>

            <AiSummaryCard
              summary={summary}
              loading={outlineLoading}
              error={outlineError}
              stale={outlineStale}
              generatedAt={outlineAt}
              onRegenerate={onRegenerate}
            />

            <AiReplyAssistant
              conversationId={conv?.id || null}
              onToast={onToast}
              onUseReply={(text) => {
                onUseAiReply?.(text);
                onToast("Reply copied to composer — review before send");
              }}
            />

            <section className="wa-card">
              <h4 className="wa-card-title">Quick Actions</h4>
              <div className="wa-qa-grid">
                <button type="button" className="wa-qa primary" disabled={taskBusy} onClick={() => createTask()}>
                  <PlusIcon size={15} /> Create Task
                </button>
                <button type="button" className="wa-qa" disabled={leadBusy} onClick={() => createLead()}>
                  <UserPlusIcon size={15} /> Create Lead
                </button>
                <button type="button" className="wa-qa" disabled={taskBusy} onClick={() => scheduleFollowUp()}>
                  <CalendarIcon size={15} /> Schedule Follow-up
                </button>
                <button
                  type="button"
                  className="wa-qa"
                  onClick={() => {
                    onToast("Open Quotations to create a quote — not auto-created from inbox");
                    navigate("/orders");
                  }}
                >
                  <FileTextIcon size={15} /> Create Quotation
                </button>
                <button type="button" className="wa-qa" onClick={openLinkDealer}>
                  <LinkIcon size={15} /> Link Dealer
                </button>
                <button
                  type="button"
                  className="wa-qa"
                  onClick={() => {
                    setAssignOpen(true);
                    loadAgents().catch(() => null);
                  }}
                >
                  <UserCogIcon size={15} /> Assign Owner
                </button>
                {onMarkSpam ? (
                  <button type="button" className="wa-qa wa-qa-spam" onClick={onMarkSpam}>
                    Mark as Spam
                  </button>
                ) : null}
              </div>
            </section>

            <section className="wa-card">
              <div className="wa-card-head">
                <h4>Recent activity / AI insights</h4>
                <button type="button" className="wa-text-btn" onClick={() => setTab("Activities")}>View All</button>
              </div>
              <div className="wa-timeline">
                {filteredActivities.slice(0, 6).map((a) => (
                  <div key={a.id} className="wa-tl-item">
                    <span className={`wa-tl-dot ${a.filter}`} />
                    <div>
                      <b>{a.kind}</b>
                      <p>{a.detail}</p>
                      <span className="wa-tl-meta">{a.who} · {fmtTime(a.at)}</span>
                    </div>
                  </div>
                ))}
                {!filteredActivities.length ? <p className="wa-hint">No activity yet.</p> : null}
              </div>
            </section>
          </>
        ) : tab === "Activities" ? (
          <section className="wa-card">
            <div className="wa-card-head">
              <h4>Activities</h4>
              <select value={activityFilter} onChange={(e) => setActivityFilter(e.target.value as typeof activityFilter)}>
                <option value="all">All</option>
                <option value="in">Inbound</option>
                <option value="out">Outbound</option>
                <option value="note">Notes</option>
                <option value="ai">AI insights</option>
              </select>
            </div>
            <div className="wa-timeline">
              {filteredActivities.map((a) => (
                <div key={a.id} className="wa-tl-item">
                  <span className={`wa-tl-dot ${a.filter}`} />
                  <div>
                    <b>{a.kind}</b>
                    <p>{a.detail}</p>
                    <span className="wa-tl-meta">{a.who} · {fmtTime(a.at)}</span>
                  </div>
                </div>
              ))}
              {!filteredActivities.length ? <p className="wa-hint">No matching activities.</p> : null}
            </div>
          </section>
        ) : tab === "Notes" ? (
          <section className="wa-card">
            <h4 className="wa-card-title">Internal notes</h4>
            <p className="wa-hint">Notes stay inside CRM — never sent to WhatsApp customers.</p>
            <textarea
              className="wa-note-input"
              rows={3}
              value={noteDraft}
              onChange={(e) => setNoteDraft(e.target.value)}
              placeholder="Add an internal note…"
            />
            <div className="wa-pactions">
              <button type="button" className="btn primary" disabled={noteSaving || !noteDraft.trim()} onClick={() => saveNote()}>
                {noteSaving ? "Saving…" : "Add note"}
              </button>
            </div>
            <div className="wa-notes-list">
              {notes.map((n) => (
                <article key={n.id} className="wa-note-item">
                  <header>
                    <b>{n.sender_name || "Employee"}</b>
                    <time>{fmtTime(n.created_at)}</time>
                  </header>
                  <p>{n.body}</p>
                </article>
              ))}
              {!notes.length ? <p className="wa-hint">No notes yet.</p> : null}
            </div>
          </section>
        ) : (
          <section className="wa-card">
            <h4 className="wa-card-title">Related records</h4>
            {relatedLoading ? <p className="wa-hint">Loading…</p> : null}
            <div className="wa-related-grid">
              <div className="wa-related-block">
                <h5>Linked Dealer</h5>
                {related?.dealer || conv.dealer ? (
                  <>
                    <Link className="wa-related-row" to={`/dealers?q=${encodeURIComponent((related?.dealer || conv.dealer)?.phone || "")}`}>
                      <b>{(related?.dealer || conv.dealer)?.name || "Dealer"}</b>
                      <span>{(related?.dealer || conv.dealer)?.phone}</span>
                    </Link>
                    <button
                      type="button"
                      className="wa-text-btn wa-delink-btn"
                      disabled={delinkingDealer}
                      onClick={() => delinkDealer().catch(() => null)}
                    >
                      {delinkingDealer ? "Delinking…" : "Delink dealer"}
                    </button>
                  </>
                ) : (
                  <button type="button" className="wa-text-btn" onClick={openLinkDealer}>Link Dealer</button>
                )}
              </div>
              <div className="wa-related-block">
                <h5>Leads</h5>
                {(related?.leads || []).map((l) => (
                  <Link key={l.id} className="wa-related-row" to={`/leads?q=${encodeURIComponent(l.phone || l.name)}`}>
                    <b>{l.name || l.phone}</b>
                    <span>{l.status}</span>
                  </Link>
                ))}
                {!related?.leads?.length ? <p className="wa-hint">No linked leads.</p> : null}
              </div>
              <div className="wa-related-block">
                <h5>Open Tasks</h5>
                {(related?.open_tasks || []).map((t) => (
                  <Link key={t.id} className="wa-related-row" to="/work">
                    <b>{t.title}</b>
                    <span>{t.status}{t.due_date ? ` · ${t.due_date}` : ""}</span>
                  </Link>
                ))}
                {!related?.open_tasks?.length ? <p className="wa-hint">No open tasks matched.</p> : null}
              </div>
              <div className="wa-related-block muted">
                <h5>Quotations / Orders / Service</h5>
                <p className="wa-hint">
                  <CheckCircleIcon size={14} /> Modules not wired to WhatsApp contact yet
                  {(related?.unavailable || []).length ? ` (${(related?.unavailable || []).join(", ")})` : ""}.
                </p>
              </div>
            </div>
          </section>
        )}
      </div>

      {dealerSearchOpen && conv ? (
        <div
          className="wa-assign-modal wa-dealer-link-modal"
          onClick={(e) => { if (e.target === e.currentTarget) closeLinkDealer(); }}
        >
          <div className="wa-assign-panel wa-dealer-link-panel" role="dialog" aria-label="Link dealer">
            <div className="wa-card-head">
              <h4>Link dealer</h4>
              <button type="button" className="wa-text-btn" onClick={() => closeLinkDealer()}>Close</button>
            </div>
            <p className="wa-hint wa-dealer-link-hint">Type name, phone, or Zoho code — picks update as you type.</p>
            <div className="wa-search wa-search-inset">
              <input
                ref={dealerSearchRef}
                value={dealerQ}
                onChange={(e) => {
                  setDealerQ(e.target.value);
                  setSelectedDealerId(null);
                }}
                placeholder="e.g. Global Digital, 98470…, dealer code"
                autoComplete="off"
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.preventDefault();
                    closeLinkDealer();
                  }
                }}
              />
            </div>
            <div className="wa-dealer-hits wa-dealer-link-hits">
              {dealerSearching ? <div className="wa-hint">Searching…</div> : null}
              {!dealerSearching && dealerQ.trim().length < 2 ? (
                <div className="wa-hint">Enter at least 2 characters to search.</div>
              ) : null}
              {!dealerSearching && dealerQ.trim().length >= 2 && !dealerHits.length ? (
                <div className="wa-hint">No similar dealers — try another spelling or last 10 digits.</div>
              ) : null}
              {dealerHits.map((d) => (
                <button
                  key={d.id}
                  type="button"
                  className={`wa-dealer-hit ${selectedDealerId === d.id ? "on" : ""}`}
                  onClick={() => setSelectedDealerId(d.id)}
                  onDoubleClick={() => linkSelectedDealer().catch(() => null)}
                >
                  <b>{d.name || d.company || `Dealer #${d.id}`}</b>
                  <span>
                    {[d.phone, d.code, d.city, d.state].filter(Boolean).join(" · ") || "—"}
                  </span>
                </button>
              ))}
            </div>
            <div className="wa-pactions">
              <button type="button" className="btn" onClick={() => closeLinkDealer()}>Cancel</button>
              <button
                type="button"
                className="btn primary"
                disabled={!selectedDealerId || linkingDealer}
                onClick={() => linkSelectedDealer()}
              >
                {linkingDealer ? "Linking…" : "Link selected dealer"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {assignOpen && conv ? (
        <div className="wa-assign-modal" onClick={(e) => { if (e.target === e.currentTarget) setAssignOpen(false); }}>
          <div className="wa-assign-panel">
            <div className="wa-card-head">
              <h4>Assign owner</h4>
              <button type="button" className="wa-text-btn" onClick={() => setAssignOpen(false)}>Close</button>
            </div>
            <div className="wa-pactions">
              <button type="button" className="btn primary" onClick={() => assignOwner(null, true)}>Assign to me</button>
              <button type="button" className="btn" onClick={() => assignOwner(null, false)}>Unassign</button>
            </div>
            <div className="wa-agent-list">
              {agents.map((a) => (
                <button key={a.id} type="button" className="wa-dealer-hit" onClick={() => assignOwner(a.id)}>
                  <b>{a.name}</b>
                  <span>
                    {typeof (a.open_conversations ?? a.open_count) === "number"
                      ? `${a.open_conversations ?? a.open_count} open`
                      : "Agent"}
                  </span>
                </button>
              ))}
              {!agents.length ? <p className="wa-hint">No agents loaded.</p> : null}
            </div>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
