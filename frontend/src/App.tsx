import { Navigate, NavLink, Route, Routes, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useEffect, useState } from "react";
import { api, clearToken, getToken, login, setToken } from "./api/client";
import Dashboard from "./pages/Dashboard";
import Leads from "./pages/Leads";
import Work from "./pages/Work";
import WhatsApp from "./pages/WhatsApp";
import Settings, { SETTINGS_TABS, parseSettingsTab, type SettingsTabId } from "./pages/Settings";
import EmployeeProfile from "./pages/EmployeeProfile";
import Placeholder from "./pages/Placeholder";
import Dealers from "./pages/Dealers";
import Products from "./pages/Products";
import Analytics from "./pages/Analytics";
import { NAV_ICONS, type NavIconId } from "./navIcons";

const NAV: { group: string; items: { to: string; label: string; icon: NavIconId }[] }[] = [
  {
    group: "",
    items: [
      { to: "/", label: "Dashboard", icon: "home" },
      { to: "/analytics", label: "Analytics", icon: "chart" },
      { to: "/whatsapp", label: "WhatsApp Inbox", icon: "whatsapp" },
      { to: "/calling", label: "Call List", icon: "phone" },
      { to: "/leads", label: "Leads", icon: "users" },
      { to: "/dealers", label: "Dealer", icon: "store" },
      { to: "/orders", label: "Quotations", icon: "file" },
      { to: "/campaigns", label: "Campaigns", icon: "megaphone" },
      { to: "/meta", label: "Meta Ads", icon: "target" },
      { to: "/assets", label: "Marketing Assets", icon: "image" },
      { to: "/work", label: "Work Management", icon: "checklist" },
      { to: "/products", label: "Products", icon: "package" },
      { to: "/performance", label: "Performance", icon: "trending" },
      { to: "/reports", label: "Reports", icon: "bars" },
      { to: "/settings", label: "Settings", icon: "gear" },
    ],
  },
];

const TITLES: Record<string, string> = Object.fromEntries(
  NAV.flatMap((g) => g.items.map((i) => [i.to === "/" ? "/" : i.to, i.label]))
);

function Login() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [err, setErr] = useState("");
  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    try {
      const data = await login(email, password);
      setToken(data.access_token);
      nav("/");
    } catch {
      setErr("Invalid login");
    }
  }
  return (
    <div className="login-wrap">
      <form
        className="login-card"
        onSubmit={onSubmit}
        autoComplete="off"
        data-1p-ignore="true"
        data-lpignore="true"
        data-form-type="other"
      >
        <h1>YesWeigh CRM</h1>
        <p>Business command centre · crm.yesweigh.in</p>
        {/* Hidden decoys reduce aggressive browser autofill */}
        <input type="text" name="prevent_autofill_user" autoComplete="username" tabIndex={-1} aria-hidden="true" style={{ position: "absolute", opacity: 0, height: 0, width: 0, pointerEvents: "none" }} />
        <input type="password" name="prevent_autofill_pass" autoComplete="current-password" tabIndex={-1} aria-hidden="true" style={{ position: "absolute", opacity: 0, height: 0, width: 0, pointerEvents: "none" }} />
        <input
          name="crm_email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="Email or Aadhaar"
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          inputMode="text"
          data-1p-ignore="true"
          data-lpignore="true"
        />
        <div className="password-field">
          <input
            name="crm_password"
            type={showPassword ? "text" : "password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            autoComplete="new-password"
            data-1p-ignore="true"
            data-lpignore="true"
          />
          <button type="button" className="eye-btn" aria-label={showPassword ? "Hide password" : "Show password"} onClick={() => setShowPassword((v) => !v)}>
            {showPassword ? (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-5 0-9.27-3.11-11-8 1.02-2.89 2.98-5.17 5.41-6.53" />
                <path d="M9.9 4.24A10.94 10.94 0 0 1 12 4c5 0 9.27 3.11 11 8a11.7 11.7 0 0 1-2.16 3.19" />
                <path d="M14.12 14.12a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
            ) : (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            )}
          </button>
        </div>
        {err && <div className="err">{err}</div>}
        <button className="btn primary" style={{ width: "100%" }}>Sign in</button>
      </form>
    </div>
  );
}

const SIDEBAR_STORAGE_KEY = "crm_sidebar_open";

function readSidebarOpen(): boolean {
  try {
    const v = localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (v === null) return false; // default: drawer OFF on first visit
    return v === "1" || v === "true";
  } catch {
    return false;
  }
}

function Shell({ children }: { children: React.ReactNode }) {
  const nav = useNavigate();
  const loc = useLocation();
  const [params, setParams] = useSearchParams();
  const [me, setMe] = useState<{ name: string; email: string; role?: string } | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(readSidebarOpen);

  useEffect(() => {
    api<{ name: string; email: string; role: string }>("/api/me")
      .then(setMe)
      .catch(() => {
        clearToken();
        nav("/login");
      });
  }, [nav]);

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_STORAGE_KEY, sidebarOpen ? "1" : "0");
    } catch {
      /* ignore quota / private mode */
    }
  }, [sidebarOpen]);

  const isInbox = loc.pathname === "/whatsapp";
  const isSettings = loc.pathname === "/settings";
  const settingsTab = parseSettingsTab(params.get("tab"));

  function setSettingsTab(id: SettingsTabId) {
    const next = new URLSearchParams(params);
    if (id === "general") next.delete("tab");
    else next.set("tab", id);
    setParams(next, { replace: true });
  }

  function logout() {
    clearToken();
    nav("/login");
  }

  function toggleSidebar() {
    setSidebarOpen((v) => !v);
  }

  return (
    <div className={`app ${sidebarOpen ? "" : "nav-collapsed"} ${isInbox ? "inbox-mode" : ""} ${isSettings ? "settings-mode" : ""}`.trim()}>
      {!sidebarOpen && (
        <button
          type="button"
          className="nav-toggle nav-toggle--reopen"
          aria-label="Open navigation"
          aria-expanded={false}
          onClick={toggleSidebar}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <line x1="3" y1="6" x2="21" y2="6" />
            <line x1="3" y1="12" x2="21" y2="12" />
            <line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>
      )}
      <aside className="sidebar" aria-hidden={!sidebarOpen}>
        <div className="sidebar-head">
          <div className="sidebar-brand" aria-label="yesweigh">
            <span className="sidebar-brand-yes">YES</span>
            <span className="sidebar-brand-weigh">WEIGH</span>
          </div>
          <button
            type="button"
            className="nav-toggle nav-toggle--close"
            aria-label="Close navigation"
            aria-expanded={true}
            onClick={toggleSidebar}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <polyline points="15 18 9 12 15 6" />
            </svg>
          </button>
        </div>
        <nav className="nav">
          {NAV.map((g) => (
            <div key={g.group || "main"}>
              {g.group ? <div className="navgroup">{g.group.toUpperCase()}</div> : null}
              {g.items.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.to === "/"} className={({ isActive }) => (isActive ? "active" : undefined)} tabIndex={sidebarOpen ? undefined : -1}>
                  <span className="nav-icon">{NAV_ICONS[item.icon]}</span>
                  <span className="nav-label">{item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidefoot">
          <div className="sidefoot-meta">
            <span className="dot" />
            <span>{me?.name || "Signed in"}</span>
            <span className="sidefoot-ver">v0.1</span>
          </div>
          <button type="button" className="btn side-logout" onClick={logout} tabIndex={sidebarOpen ? undefined : -1}>Logout</button>
        </div>
      </aside>
      <main className="main">
        {isSettings && (
          <header className="top settings-top">
            <div className="emp-profile-tabs settings-heading-tabs" role="tablist" aria-label="Settings">
              {SETTINGS_TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  role="tab"
                  aria-selected={settingsTab === t.id}
                  className={settingsTab === t.id ? "active" : ""}
                  onClick={() => setSettingsTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </header>
        )}
        <div className={`content ${isInbox ? "content-inbox" : ""}`}>{children}</div>
      </main>
    </div>
  );
}

function Private({ children }: { children: React.ReactNode }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <Shell>{children}</Shell>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Private><Dashboard /></Private>} />
      <Route path="/analytics" element={<Private><Analytics /></Private>} />
      <Route path="/leads" element={<Private><Leads /></Private>} />
      <Route path="/work" element={<Private><Work /></Private>} />
      <Route path="/whatsapp" element={<Private><WhatsApp /></Private>} />
      <Route path="/employees" element={<Navigate to="/settings?tab=employees" replace />} />
      <Route path="/employees/:id" element={<Private><EmployeeProfile /></Private>} />
      <Route path="/settings" element={<Private><Settings /></Private>} />
      <Route path="/dealers" element={<Private><Dealers /></Private>} />
      <Route path="/products" element={<Private><Products /></Private>} />
      {[
        "/customers", "/calling", "/orders", "/campaigns", "/meta", "/assets",
        "/calendar", "/performance", "/reports",
      ].map((p) => (
        <Route key={p} path={p} element={<Private><Placeholder title={TITLES[p] || p} /></Private>} />
      ))}
    </Routes>
  );
}
