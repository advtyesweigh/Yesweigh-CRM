import { useEffect, useState, type CSSProperties } from "react";
import { api } from "../api/client";

type Category = {
  id: string;
  name: string;
  description: string;
  display_order: number;
  product_count: number;
  image_url: string;
  is_weighing_scale: boolean;
};

type Product = {
  id: string;
  name: string;
  sku: string;
  description: string;
  unit: string;
  rate: number | null;
  stock: number | null;
  stock_status: string;
  image_url: string;
  image_urls: string[];
  category_id: string;
  category_name: string;
  status: string;
  model_number: string;
  discontinued_soon: boolean;
  new_arrival: boolean;
  display_order: number;
};

type Browse = {
  categories: Category[];
  products: Product[];
  total_products: number;
  synced_at: string | null;
};

const THEMES = [
  { bg: "#e8f4fd", accent: "#1d4f8c", badge: "#cce5f8" },
  { bg: "#e8faf0", accent: "#0f6b42", badge: "#c4edd8" },
  { bg: "#fff4e8", accent: "#b45309", badge: "#fde4c8" },
  { bg: "#f3e8ff", accent: "#6b21a8", badge: "#e4d0fc" },
  { bg: "#fef3e8", accent: "#c2410c", badge: "#fddfc8" },
  { bg: "#e8f8f8", accent: "#0f766e", badge: "#c8eeec" },
  { bg: "#fce8f0", accent: "#9d174d", badge: "#f8d4e4" },
  { bg: "#eef2ff", accent: "#3730a3", badge: "#d8ddfc" },
  { bg: "#fef9e8", accent: "#92400e", badge: "#fcefc8" },
  { bg: "#f0fdf4", accent: "#166534", badge: "#d8f5e0" },
];

function themeAt(i: number) {
  return THEMES[i % THEMES.length]!;
}

function fmtPrice(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function fmtStock(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const v = Number(n);
  const s = Number.isInteger(v) ? String(v) : v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  return `${s} nos`;
}

function imgSrc(url: string): string {
  if (!url) return "";
  if (url.startsWith("http") || url.startsWith("/")) return url;
  return url;
}

export default function Products() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [categoryId, setCategoryId] = useState("");
  const [selected, setSelected] = useState<Product | null>(null);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr("");
      try {
        const params = new URLSearchParams();
        if (categoryId) params.set("category_id", categoryId);
        if (q.trim()) params.set("q", q.trim());
        const data = await api<Browse>(`/api/catalog/browse?${params}`);
        if (cancelled) return;
        setCategories(data.categories || []);
        setProducts(data.products || []);
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : "Failed to load catalog");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [categoryId, q]);

  const showProducts = Boolean(categoryId || q.trim());

  return (
    <>
      <div className="heading">
        <div>
          <h1>Products</h1>
        </div>
      </div>

      <div className="catalog-toolbar">
        {(showProducts || selected) && (
          <button
            type="button"
            className="catalog-back"
            onClick={() => {
              if (selected) {
                setSelected(null);
                return;
              }
              setCategoryId("");
              setQ("");
            }}
          >
            ← {selected ? "Back to products" : "All categories"}
          </button>
        )}
        <input
          className="catalog-search"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setSelected(null);
          }}
          placeholder="Search name, SKU, category…"
        />
      </div>

      {err && <div className="panel" style={{ padding: 16, color: "var(--red)" }}>{err}</div>}
      {loading && !categories.length && !products.length && (
        <div className="panel" style={{ padding: 20, color: "var(--muted)" }}>Loading catalog…</div>
      )}

      {!loading && !categories.length && !err && (
        <div className="panel" style={{ padding: 20, color: "var(--muted)" }}>
          No catalog yet. Run Firebase sync on the VPS:
          <code style={{ display: "block", marginTop: 8 }}>
            python -m app.sync_firebase_catalog
          </code>
        </div>
      )}

      {selected ? (
        <ProductDetail product={selected} onClose={() => setSelected(null)} />
      ) : showProducts ? (
        <div className="catalog-product-grid">
          {products.map((p) => (
            <button
              key={p.id}
              type="button"
              className="catalog-product-card"
              onClick={() => setSelected(p)}
            >
              <div className="catalog-product-card__top">
                <span className="catalog-product-card__sku">{p.sku || "—"}</span>
                {p.stock_status === "in_stock" || (p.stock != null && p.stock > 0) ? (
                  <span className="catalog-product-card__ok" aria-hidden>✓</span>
                ) : null}
              </div>
              <div className="catalog-product-card__media">
                {p.image_url ? (
                  <img src={imgSrc(p.image_url)} alt="" loading="lazy" />
                ) : (
                  <div className="catalog-product-card__ph">No image</div>
                )}
                {p.discontinued_soon && (
                  <span className="catalog-product-card__banner">Discontinued soon</span>
                )}
                {p.model_number && (
                  <span className="catalog-product-card__model">{p.model_number}</span>
                )}
              </div>
              <div className="catalog-product-card__body">
                <h3>{p.name}</h3>
                <div className="catalog-product-card__meta">
                  <div className="catalog-product-card__price">
                    <span className="rupee">₹</span>
                    {fmtPrice(p.rate)}
                  </div>
                  <span className="catalog-product-card__stock">{fmtStock(p.stock)}</span>
                </div>
              </div>
            </button>
          ))}
          {!loading && !products.length && (
            <div className="panel" style={{ padding: 20, gridColumn: "1 / -1", color: "var(--muted)" }}>
              No products in this view.
            </div>
          )}
        </div>
      ) : (
        <div className="catalog-category-grid">
          {categories.map((c, i) => {
            const t = themeAt(i);
            return (
              <button
                key={c.id}
                type="button"
                className="catalog-category-card"
                style={
                  {
                    "--cat-bg": t.bg,
                    "--cat-accent": t.accent,
                    "--cat-badge": t.badge,
                  } as CSSProperties
                }
                onClick={() => setCategoryId(c.id)}
              >
                <div className="catalog-category-card__text">
                  <h3>{c.name}</h3>
                  <p>{c.description || `Browse ${c.name} products`}</p>
                  <span className="catalog-category-card__count">
                    {c.product_count} {c.product_count === 1 ? "item" : "items"}
                  </span>
                </div>
                <div className="catalog-category-card__img">
                  {c.image_url ? (
                    <img src={imgSrc(c.image_url)} alt="" loading="lazy" />
                  ) : (
                    <div className="catalog-category-card__ph" />
                  )}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}

function ProductDetail({ product, onClose }: { product: Product; onClose: () => void }) {
  const gallery = product.image_urls?.length ? product.image_urls : product.image_url ? [product.image_url] : [];
  const [idx, setIdx] = useState(0);
  const src = gallery[idx] || "";

  return (
    <div className="catalog-detail panel">
      <div className="catalog-detail__media">
        {src ? <img src={imgSrc(src)} alt="" /> : <div className="catalog-product-card__ph">No image</div>}
        {gallery.length > 1 && (
          <div className="catalog-detail__thumbs">
            {gallery.map((u, i) => (
              <button key={u + i} type="button" className={i === idx ? "active" : ""} onClick={() => setIdx(i)}>
                <img src={imgSrc(u)} alt="" />
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="catalog-detail__info">
        <p className="catalog-detail__sku">{product.sku || "—"}</p>
        <h2>{product.name}</h2>
        <p className="catalog-detail__cat">{product.category_name}</p>
        <div className="catalog-product-card__meta" style={{ marginTop: 12 }}>
          <div className="catalog-product-card__price">
            <span className="rupee">₹</span>
            {fmtPrice(product.rate)}
          </div>
          <span className="catalog-product-card__stock">{fmtStock(product.stock)}</span>
        </div>
        {product.model_number && <p className="catalog-detail__model">Model: {product.model_number}</p>}
        {product.description && (
          <div className="catalog-detail__desc">
            <h4>Details</h4>
            <p>{product.description}</p>
          </div>
        )}
        <button type="button" className="primary-btn" style={{ marginTop: 16 }} onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}
