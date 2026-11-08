// Server-rendered English website: marketing pages, public data pages, docs, account and legal pages.
import type { User } from "./accounts";
import { QUOTA, RPM } from "./auth";
import type { BillingView } from "./billing";
import { scrubProposalTitle } from "./data";

export const esc = (v: unknown) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

// ---------- Formatting ----------

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const fmtNum = (v: unknown, digits = 0) => (isNum(v) ? v.toLocaleString("en-US", { maximumFractionDigits: digits }) : "—");
const fmtPct = (v: unknown, digits = 2) => (isNum(v) ? `${v.toFixed(digits)}%` : "—");
const fmtPt = (v: unknown) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}pt` : "—");

type Scale = { div: number; unit: string };
const SCALES: Scale[] = [{ div: 1e12, unit: "tn" }, { div: 1e9, unit: "bn" }, { div: 1e6, unit: "m" }];

/** One unit for a whole column or table, chosen from its largest value. */
function scaleFor(values: unknown[]): Scale {
  const max = Math.max(0, ...values.filter(isNum).map(Math.abs));
  return SCALES.find((s) => max >= s.div * 10) ?? (max >= 1e9 ? SCALES[1] : SCALES[2]);
}
const fmtJpy = (v: unknown, s: Scale, digits = s.unit === "m" ? 0 : 1) =>
  isNum(v) ? `${v < 0 ? "−" : ""}¥${(Math.abs(v) / s.div).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${s.unit}` : "—";
const fmtJpyAuto = (v: unknown) => (isNum(v) ? fmtJpy(v, SCALES.find((s) => Math.abs(v) >= s.div) ?? SCALES[2]) : "—");
const fmtUsd = (v: unknown) => (isNum(v) ? (v >= 1e9 ? `$${(v / 1e9).toFixed(2)}bn` : `$${(v / 1e6).toFixed(1)}m`) : "—");

// ---------- Design tokens and base styles ----------

const FONTS =
  "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap";

const CSS = `
/* Layout: a filing ledger. One 1120px column, data set in mono, ink-indigo for structure, vermilion only for alerts. */
:root{
  --bg:#f5f6f8;--surface:#ffffff;--sunken:#eceef2;--ink:#121722;--muted:#596273;--faint:#8a93a3;--line:#dce0e7;
  --accent:#2c3c8f;--accent-ink:#ffffff;--accent-soft:#e7eaf7;--seal:#c63d26;--seal-soft:#fbe9e5;--up:#1b7a4c;--down:#b03a2a;
  --display:"Bricolage Grotesque","Avenir Next",system-ui,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  --r:6px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d1117;--surface:#141a23;--sunken:#0a0e14;--ink:#e6e9ef;--muted:#9ba4b4;--faint:#6c7586;--line:#252d3a;
  --accent:#93a2f2;--accent-ink:#0d1117;--accent-soft:#1b2342;--seal:#f07e66;--seal-soft:#3a1d18;--up:#52c58f;--down:#f08a76;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 var(--body)}
a{color:var(--accent);text-underline-offset:2px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
.wrap{max-width:1120px;margin:0 auto;padding-left:max(20px,env(safe-area-inset-left,0px));padding-right:max(20px,env(safe-area-inset-right,0px))}
h1,h2,h3{font-family:var(--display);font-weight:700;letter-spacing:-.01em;text-wrap:balance;margin:0}
h1{font-size:clamp(30px,5.2vw,52px);line-height:1.08}
h2{font-size:clamp(22px,2.6vw,28px);line-height:1.2}
h3{font-size:18px;line-height:1.3;font-weight:500}
p{margin:0}
.lead{font-size:clamp(17px,1.6vw,19px);color:var(--muted);max-width:62ch}
.muted{color:var(--muted)}.faint{color:var(--faint)}.small{font-size:14px}.xs{font-size:12.5px}
.mono,code,pre,kbd{font-family:var(--mono);font-size:.9em}
.eyebrow{font:500 12px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.stack{display:flex;flex-direction:column;gap:16px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
section{padding-block:40px;border-top:1px solid var(--line)}
section.first{border-top:0}
.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:20px}

/* Header */
.top{position:sticky;top:env(safe-area-inset-top,0px);z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:30;background:var(--bg)}
.top .wrap{display:flex;align-items:center;gap:20px;min-height:60px;flex-wrap:wrap;padding-block:8px}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;font:700 18px/1 var(--display)}
.brand svg{flex:none}
.nav{display:flex;gap:4px;flex-wrap:wrap;margin-left:auto;align-items:center}
.nav a{color:var(--ink);text-decoration:none;padding:6px 10px;border-radius:var(--r);font-size:15px}
.nav a:hover,.nav a[aria-current=page]{background:var(--sunken)}
.search{display:flex;flex:1 1 220px;max-width:340px}
.search input{width:100%;min-width:0;font:15px var(--body);padding:8px 12px;border:1px solid var(--line);border-radius:var(--r);background:var(--surface);color:var(--ink)}
.search input::placeholder{color:var(--faint)}
@media (max-width:700px){
  .top{position:static;backdrop-filter:none;background:var(--bg)}
  .top .wrap{gap:10px}
  .search{order:3;max-width:none;flex-basis:100%}
  .nav{order:2;margin-left:-10px;width:calc(100% + 20px);gap:0}
  .nav a{white-space:nowrap;font-size:14.5px}
}

/* Buttons and chips */
.btn{display:inline-flex;align-items:center;gap:8px;font:600 15px/1 var(--body);padding:12px 18px;border-radius:var(--r);border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);text-decoration:none;cursor:pointer}
.btn:hover{filter:brightness(1.08);text-decoration:none}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
.btn.ghost:hover{background:var(--sunken);filter:none}
.chip{display:inline-flex;align-items:center;gap:6px;font:500 12px/1 var(--mono);padding:4px 7px;border-radius:4px;background:var(--sunken);color:var(--muted);white-space:nowrap}
.chip.accent{background:var(--accent-soft);color:var(--accent)}
.chip.seal{background:var(--seal-soft);color:var(--seal)}
.chip.up{color:var(--up)}.chip.down{color:var(--down)}
.plan{font:600 11px/1 var(--mono);letter-spacing:.05em;text-transform:uppercase;padding:3px 6px;border-radius:3px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.plan.pro{border-color:var(--accent);color:var(--accent)}.plan.business{border-color:var(--ink);color:var(--ink)}

/* Panels and tables */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:14.5px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font:500 12px/1.3 var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--muted);white-space:nowrap;background:var(--surface)}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
td.date{font-family:var(--mono);white-space:nowrap;color:var(--muted);font-size:13.5px}
.ja{font-family:var(--body);color:var(--muted);font-size:13px}
pre{background:var(--sunken);border:1px solid var(--line);border-radius:var(--r);padding:16px;overflow-x:auto;font-size:13.5px;line-height:1.55;margin:0}
code{background:var(--sunken);padding:1px 5px;border-radius:3px}
pre code{background:none;padding:0}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.kv>div{background:var(--surface);padding:14px 16px;display:flex;flex-direction:column;gap:4px;min-width:0}
.kv dt{font:500 11.5px/1.2 var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.kv dd{margin:0;font:500 20px/1.2 var(--mono);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.kv dd small{font-size:12px;color:var(--faint)}

/* Event feed */
.feed{list-style:none;margin:0;padding:0}
.feed li{display:grid;grid-template-columns:96px minmax(0,1fr);gap:4px 16px;padding:14px 16px;border-bottom:1px solid var(--line)}
.feed li:last-child{border-bottom:0}
.feed .when{font:13px/1.5 var(--mono);color:var(--muted)}
.feed .what{display:flex;flex-direction:column;gap:6px;min-width:0}
.feed .meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--muted)}
.meter{display:block;width:100%;max-width:340px;height:26px}
.meter .track{fill:var(--sunken)}.meter .fill{fill:var(--accent)}.meter .fill.seal{fill:var(--seal)}
.meter .prev{stroke:var(--ink);stroke-width:1.5}.meter .tick{stroke:var(--faint);stroke-width:1}
.meter text{font:10px var(--mono);fill:var(--faint)}
@media (max-width:560px){.feed li{grid-template-columns:1fr}}

/* Charts */
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.chart{padding:16px}
.chart svg{display:block;width:100%;height:auto}
.chart .bar{fill:var(--accent)}.chart .bar.neg{fill:var(--down)}
.chart .base{stroke:var(--line)}.chart text{font:11px var(--mono);fill:var(--muted)}
.chart .val{fill:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:6px}.legend span{display:inline-flex;align-items:center;gap:6px}

/* Landing */
.hero{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:48px;align-items:start;padding-block:56px 48px}
.hero .stack{gap:22px}
.hero .ctas{display:flex;gap:12px;flex-wrap:wrap}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:12px}
.ind-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));gap:12px}
.ind{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px;display:flex;flex-direction:column;gap:8px;min-width:0}
.ind .k{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.ind .v{font:500 28px/1.1 var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}.ind .v small{font-size:13px;color:var(--muted);margin-left:4px}
.gauge{height:6px;border-radius:3px;background:var(--sunken);position:relative;overflow:hidden}.gauge i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:3px}
.quote{border-left:2px solid var(--line);padding-left:10px;font-size:13.5px;color:var(--muted)}
.stats{display:flex;gap:28px;flex-wrap:wrap;font-size:14px;color:var(--muted)}
.stats b{display:block;font:500 22px/1.2 var(--mono);color:var(--ink);font-variant-numeric:tabular-nums}
.feature{display:grid;grid-template-columns:minmax(0,.9fr) minmax(0,1.1fr);gap:40px;align-items:start}
.feature>*{min-width:0}
.feature .stack{gap:14px}
.feature ul{margin:0;padding-left:18px;color:var(--muted)}
.feature li+li{margin-top:6px}
@media (max-width:860px){.hero,.feature{grid-template-columns:1fr;gap:28px}.hero{padding-block:36px 28px}}

/* Pricing */
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.plans .panel{padding:22px;display:flex;flex-direction:column;gap:14px}
.plans .panel.featured{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.price{font:500 34px/1 var(--mono)}.price small{font-size:14px;color:var(--muted)}
.plans ul{margin:0;padding-left:18px;font-size:14.5px}.plans li+li{margin-top:6px}

/* Forms */
.field{display:flex;gap:10px;flex-wrap:wrap}
.field input{flex:1 1 240px;max-width:380px;min-width:0;font:16px var(--body);padding:11px 12px;border:1px solid var(--line);border-radius:var(--r);background:var(--surface);color:var(--ink)}
.notice{padding:14px 16px;border-radius:var(--r);background:var(--accent-soft);border:1px solid var(--line)}
.notice.warn{background:var(--seal-soft)}

/* Docs and prose */
.docs{display:grid;grid-template-columns:200px minmax(0,1fr);gap:40px;padding-block:40px}
.docs nav{position:sticky;top:calc(env(safe-area-inset-top,0px) + 80px);align-self:start;display:flex;flex-direction:column;gap:2px;font-size:14px}
.docs nav a{color:var(--muted);text-decoration:none;padding:4px 0}.docs nav a:hover{color:var(--ink)}
.prose{max-width:82ch;min-width:0;display:flex;flex-direction:column;gap:16px}
.prose h2{margin-top:28px;padding-top:12px}.prose h3{margin-top:8px}
.prose ul,.prose ol{margin:0;padding-left:20px}.prose li+li{margin-top:4px}
.endpoint{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;padding:14px 16px;border-bottom:1px solid var(--line)}
.endpoint:last-child{border-bottom:0}
.endpoint .path{font:500 14px/1.4 var(--mono);overflow-wrap:anywhere}
.endpoint .path b{color:var(--accent);font-weight:600;margin-right:8px}
.endpoint p,.endpoint table,.endpoint pre,.endpoint details{grid-column:1/-1;min-width:0}
.endpoint p{color:var(--muted);font-size:14.5px}
.endpoint .params{font-size:13.5px}.endpoint .params td{padding:4px 12px 4px 0;border:0}.endpoint .params td:first-child{white-space:nowrap}
.endpoint pre{font-size:12.5px;padding:12px}
details summary{cursor:pointer;font-size:13.5px;color:var(--accent);padding:4px 0}details pre{margin-top:6px;max-height:420px;overflow:auto;font-size:12.5px}
.prose>h2{scroll-margin-top:80px}
@media (max-width:820px){.docs{grid-template-columns:minmax(0,1fr)}.docs nav{position:static;flex-direction:row;flex-wrap:wrap;gap:4px 14px}}

/* Company header */
.co-head{display:flex;flex-direction:column;gap:10px;padding-block:36px 24px}
.co-head .ids{display:flex;gap:8px;flex-wrap:wrap}
.co-title{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
.co-title .tk{font:500 clamp(20px,3vw,28px)/1 var(--mono);color:var(--muted)}

/* Footer */
footer{margin-top:56px;border-top:1px solid var(--line);background:var(--surface)}
footer .wrap{display:grid;grid-template-columns:minmax(0,1.4fr) repeat(3,minmax(0,1fr));gap:28px;padding-block:36px 44px;font-size:14px}
footer h4{font:500 12px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
footer ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
footer a{color:var(--ink);text-decoration:none}footer a:hover{text-decoration:underline}
@media (max-width:760px){footer .wrap{grid-template-columns:1fr 1fr}footer .about{grid-column:1/-1}}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
`;

const LOGO = `<svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><rect x="1" y="1" width="24" height="24" rx="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="M7 8h12M7 13h12M7 18h7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="19.5" cy="18" r="2.6" fill="#c63d26"/></svg>`;
export const FAVICON = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 26 26"><rect x="1" y="1" width="24" height="24" rx="4" fill="#fff" stroke="#2c3c8f" stroke-width="2"/><path d="M7 8h12M7 13h12M7 18h7" stroke="#2c3c8f" stroke-width="2" stroke-linecap="round"/><circle cx="19.5" cy="18" r="2.6" fill="#c63d26"/></svg>`;

const NAV = [
  ["/events", "Ownership changes"],
  ["/indicators", "Indicators"],
  ["/cross-holdings", "Cross-holdings"],
  ["/docs", "Docs"],
  ["/pricing", "Pricing"],
];

type PageOpts = { description?: string; path?: string; noindex?: boolean };

export function page(title: string, body: string, user: User | null, opts: PageOpts | string = {}) {
  const o: PageOpts = typeof opts === "string" ? { description: opts } : opts;
  const desc = o.description || "Japanese listed-company filings as English JSON for developers and AI agents: ownership changes, cross-shareholdings, financials. REST API, MCP and webhooks.";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>${esc(title)}</title><meta name="description" content="${esc(desc)}">
<meta property="og:title" content="${esc(title)}"><meta property="og:description" content="${esc(desc)}"><meta property="og:type" content="website">
${o.noindex ? `<meta name="robots" content="noindex">` : ""}
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="${FONTS}">
<style>${CSS}</style></head><body>
<header class="top"><div class="wrap">
<a class="brand" href="/">${LOGO}japanfilings</a>
<form class="search" action="/search" method="get" role="search"><label class="xs" for="q" style="position:absolute;left:-9999px">Search companies</label><input id="q" name="q" type="search" placeholder="Search companies: 7203, Toyota…" autocomplete="off"></form>
<nav class="nav">${NAV.map(([href, label]) => `<a href="${href}"${o.path === href ? ` aria-current="page"` : ""}>${label}</a>`).join("")}
${user ? `<a href="/dashboard"${o.path === "/dashboard" ? ` aria-current="page"` : ""}>Dashboard</a>` : `<a href="/login">Sign in</a>`}</nav>
</div></header>
<main>${body}</main>
<footer><div class="wrap">
<div class="about stack" style="gap:10px"><a class="brand" href="/">${LOGO}japanfilings</a>
<p class="muted small">English summaries and extracts of public filings by Japanese listed companies. Reference data, not investment advice, a recommendation or an offer. We do not publish share prices. Event types, ticker matches, net share counts and summaries are our own processing, not official statements. Not affiliated with any government body.</p></div>
<div><h4>Product</h4><ul><li><a href="/docs">Documentation</a></li><li><a href="/pricing">Pricing</a></li><li><a href="/openapi.json">OpenAPI spec</a></li><li><a href="/v1/status">Data status</a></li></ul></div>
<div><h4>Data</h4><ul><li><a href="/events">Ownership changes</a></li><li><a href="/cross-holdings">Cross-shareholdings</a></li><li><a href="/indicators">Indicators</a></li><li><a href="/sources">Sources and licences</a></li></ul></div>
<div><h4>Legal</h4><ul><li><a href="/terms">Terms of use</a></li><li><a href="/privacy">Privacy</a></li><li><a href="/contact">Contact and corrections</a></li></ul></div>
</div></footer>
</body></html>`;
}

// ---------- Components ----------

export const EVENT_LABEL: Record<string, string> = {
  new_stake: "New 5% stake", stake_increase: "Increase", stake_decrease: "Decrease", exit_below_5pct: "Below 5%",
  threshold_crossed: "Threshold", stated_may_make_proposals: "Stated it may make proposals", purpose_changed: "Purpose change", amendment: "Amendment",
};
const eventChip = (type: string) =>
  `<span class="chip ${type === "stake_increase" || type === "new_stake" ? "accent" : ""}">${esc(EVENT_LABEL[type] ?? type)}</span>`;

/** Holding ratio on a 0–50% (or 0–100%) ruler with evenly spaced gridlines; previous ratio as a tick. */
function stakeMeter(now: unknown, prev: unknown, alert = false) {
  if (!isNum(now)) return "";
  const max = Math.max(now, isNum(prev) ? prev : 0) > 50 ? 100 : 50;
  const W = 340, x = (v: number) => 1 + (Math.min(v, max) / max) * (W - 2);
  const ticks = max === 100 ? [0, 25, 50, 75, 100] : [0, 10, 20, 30, 40, 50];
  return `<svg class="meter" viewBox="0 0 ${W} 26" role="img" aria-label="Holding ${fmtPct(now)}${isNum(prev) ? `, previously ${fmtPct(prev)}` : ""}">
<rect class="track" x="0" y="2" width="${W}" height="10" rx="2"/><rect class="fill${alert ? " seal" : ""}" x="0" y="2" width="${x(now).toFixed(1)}" height="10" rx="2"/>
${ticks.map((t) => `<line class="tick" x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="0" y2="14"/><text x="${Math.min(x(t), W - 14).toFixed(1)}" y="25" text-anchor="${t === max ? "end" : "middle"}">${t}</text>`).join("")}
${isNum(prev) ? `<line class="prev" x1="${x(prev).toFixed(1)}" x2="${x(prev).toFixed(1)}" y1="0" y2="14"/>` : ""}</svg>`;
}

export function eventList(events: any[], opts: { showCompany?: boolean } = {}) {
  if (!events.length) return `<p class="muted" style="padding:16px">No ownership changes of this kind in this period.</p>`;
  return `<ul class="feed">${events.map((e) => {
    const company = e.issuer_ticker ? `<a href="/companies/${esc(e.issuer_ticker)}">${esc(e.issuer_name_en ?? e.issuer_name_ja)}</a> <span class="mono xs">${esc(e.issuer_ticker)}</span>` : esc(e.issuer_name_ja);
    return `<li><div class="when">${esc(e.submit_date)}</div><div class="what">
<div class="meta">${eventChip(e.type)}${opts.showCompany !== false ? `<span>${company}</span>` : ""}${isNum(e.change_pt) && e.type !== "threshold_crossed" ? `<span class="chip ${e.change_pt > 0 ? "up" : "down"}">${fmtPt(e.change_pt)}</span>` : ""}</div>
<p>${esc(e.summary_en)}</p>
${e.type !== "amendment" ? stakeMeter(e.holding_ratio_pct, e.previous_ratio_pct) : ""}
<div class="meta xs"><span class="mono">${esc(e.doc_id)}</span>${e.filer_edinet_code ? `<a href="/investors/${esc(e.filer_edinet_code)}">Filer profile</a>` : ""}${e.source_url ? `<a href="${esc(e.source_url)}" rel="noopener">Original filing (PDF, Japanese)</a>` : ""}</div>
</div></li>`;
  }).join("")}</ul>`;
}

/** Bars to one scale from a zero baseline; negative years drop below it. */
function barChart(title: string, points: { label: string; value: number | null }[]) {
  const vals = points.map((p) => p.value).filter(isNum);
  if (!vals.length) return "";
  const max = Math.max(...vals.map(Math.abs));
  const s = SCALES.find((x) => max >= x.div) ?? SCALES[2];
  const short = (v: number) => (v / s.div).toLocaleString("en-US", { maximumFractionDigits: Math.abs(v / s.div) < 10 ? 2 : Math.abs(v / s.div) < 100 ? 1 : 0 });
  const W = 320, H = 150, top = 20, bottom = 24, plotH = H - top - bottom;
  const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals), span = hi - lo || 1;
  const y = (v: number) => top + ((hi - v) / span) * plotH;
  const bw = (W - 20) / points.length;
  const bars = points.map((p, i) => {
    const cx = 10 + bw * i + bw / 2;
    if (!isNum(p.value)) return `<text x="${cx}" y="${H - 8}" text-anchor="middle">${esc(p.label)}</text>`;
    const y0 = y(0), y1 = y(p.value), h = Math.max(Math.abs(y1 - y0), 1);
    const neg = p.value < 0;
    return `<g><title>${esc(p.label)}: ${fmtJpy(p.value, s)}</title><rect class="bar${neg ? " neg" : ""}" x="${(cx - bw * 0.3).toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${(bw * 0.6).toFixed(1)}" height="${h.toFixed(1)}" rx="2"/>
<text class="val" x="${cx}" y="${(neg ? y1 + 12 : y1 - 5).toFixed(1)}" text-anchor="middle">${short(p.value)}</text>
<text x="${cx}" y="${H - 8}" text-anchor="middle">${esc(p.label)}</text></g>`;
  });
  return `<figure class="panel chart" style="margin:0"><figcaption class="eyebrow" style="margin-bottom:8px">${esc(title)} · ¥${s.unit}</figcaption>
<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)} by fiscal year"><line class="base" x1="0" x2="${W}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>${bars.join("")}</svg></figure>`;
}

const companyLink = (t: string | null, en: string | null, ja: string | null) =>
  t ? `<a href="/companies/${esc(t)}">${esc(en ?? ja ?? t)}</a> <span class="mono xs faint">${esc(t)}</span>` : `<span>${esc(ja)}</span>`;

const PLANS = [
  { name: "Starter", price: "$9", plan: "starter" as const, note: "Planned price, not on sale yet", items: [
    "Every dataset, endpoint and MCP tool", "Full history on file: 5% filings and ownership changes since November 2021", "Tender offers, shareholder votes, disclosed trades, cross-shareholdings, investor look-up",
    "U.S. fund holdings (SEC Form N-PORT), company briefs, screener", "Webhooks for ownership changes", "20,000 requests a month, 60 a minute"] },
  { name: "Pro", price: "$39", plan: "pro" as const, note: "Planned price, not on sale yet", featured: true, items: [
    "Everything in Starter", "300,000 requests a month, 600 a minute", "For production apps and agents serving many users"] },
];

// Paid plans go on sale only when every billing setting is present; until then the buttons say so.
function buyButton(plan: "starter" | "pro", open: boolean, user: User | null) {
  if (!open) return `<button class="btn ghost" type="button" disabled>Paid plans are not open yet</button>`;
  if (!user) return `<a class="btn" href="/login">Sign in to subscribe</a>`;
  return `<form method="post" action="/billing/checkout"><input type="hidden" name="plan" value="${plan}"><button class="btn" type="submit">Subscribe to ${plan === "pro" ? "Pro" : "Starter"}</button></form>`;
}

function pricingCards(open = false, user: User | null = null) {
  return `<div class="plans">${PLANS.map((p) => `<div class="panel${p.featured ? " featured" : ""}">
<h3>${p.name}</h3>
<p class="price">${p.price}<small> / month</small></p>
<p class="muted small">${esc(open ? "On sale" : p.note)} · ${fmtNum(QUOTA[p.plan])} requests a month</p>
<ul>${p.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>${buyButton(p.plan, open, user)}</div>`).join("")}</div>`;
}

const BILLING_NOTE = `<p class="notice small"><b>Not launched yet.</b> Signup (email, one-time link, then a key on the dashboard) works only on a local test deployment for now, and opens to the public at launch. Every plan is paid, from $9 a month; an account without a plan cannot call the API. Billing is off, so no plan can be bought yet; the prices shown are what we plan to charge.</p>`;
const OPEN_NOTE = `<p class="notice small"><b>How signup works:</b> enter your email, follow the one-time link, and create a key on the dashboard. Paid plans are sold through Paddle, our merchant of record, which handles payment and VAT or sales tax. A plan change applies to your existing key.</p>`;

// ---------- Landing ----------

export type SiteStats = { companies: number; reports: number; filings5: number; events: number; crossHoldings: number; latest: string | null; earliest?: string | null };
export type LandingExtras = { trades?: any; funds?: any; brief?: any; billingOpen?: boolean };

const JSON_EVENT_KEYS = ["type", "submit_date", "issuer_ticker", "issuer_name_en", "filer_name_en", "holding_ratio_pct", "previous_ratio_pct", "stated_may_make_proposals", "summary_en", "doc_id"];

export function landing(user: User | null, origin: string, stats: SiteStats, events: any[], toyota: any | null, extras: LandingExtras = {}) {
  const holds = (toyota?.holds ?? []).filter((h: any) => h.kind === "specified").slice(0, 5);
  const s = scaleFor(holds.map((h: any) => h.book_value_jpy));
  const sample = events.find((e) => e.type === "stated_may_make_proposals") ?? events.find((e) => e.type === "stake_increase") ?? events[0];
  const sampleJson = sample ? Object.fromEntries(JSON_EVENT_KEYS.map((k) => [k, sample[k]])) : null;
  const flows = (extras.trades?.by_holder ?? []).filter((h: any) => isNum(h.disclosed_vwap_jpy)).slice(0, 3);
  const funds = extras.funds, topFunds = (funds?.top_funds ?? []).slice(0, 3);
  const brief = extras.brief, rl = brief?.rate_lens, lr = brief?.latest_results;
  const o = esc(origin);
  return page("japanfilings: Japan ownership and activism data for funds and AI agents", `
<div class="wrap"><div class="hero">
<div class="stack">
<span class="eyebrow">Japan ownership and activism data · English · REST, MCP, webhooks</span>
<h1>Who is buying into Japanese companies, and what they say they will do.</h1>
<p class="lead">5% stakes and stated proposal intentions, tender offers, shareholder votes, disclosed trades and cross-shareholdings for ${fmtNum(stats.companies)} listed Japanese companies, from public EDINET filings, in English JSON for funds, researchers and AI agents. Bring your own prices: every company carries a FIGI and common ticker symbols to join them.</p>
<p class="notice small"><b>Not launched yet; billing is off.</b> This is reference data from public filings. It is not investment advice, and we publish no share prices.</p>
<div class="ctas"><a class="btn" href="/pricing">See plans, from $9 a month</a><a class="btn ghost" href="/docs">Read the docs</a></div>
<div class="stats"><div><b>${fmtNum(stats.reports)}</b>annual reports parsed</div><div><b>${fmtNum(stats.events)}</b>ownership changes</div><div><b>${fmtNum(stats.crossHoldings)}</b>cross-holdings matched</div></div>
</div>
<div class="panel"><div class="section-head" style="padding:14px 16px 0;margin:0"><span class="eyebrow">Latest ownership changes</span><a class="small" href="/events">All changes →</a></div>
${eventList(events.slice(0, 4))}</div>
</div></div>

<section><div class="wrap stack" style="gap:20px">
<div class="stack" style="gap:8px"><span class="eyebrow">Why not read EDINET directly?</span><h2>You can. It is free, and the documents are mostly in Japanese.</h2>
<p class="muted" style="max-width:68ch">EDINET gives you documents. To use them from abroad you still have to parse the XBRL, match names to tickers, compare each report with the one before it, and read the labels in English. That is the work this service does for the fields it covers.</p></div>
<div class="cards">
<div class="ind"><h3>Each filing compared with the last</h3><p class="small muted">A 5% report states a holding. We work out the event: a new stake, a rise, a crossing of 10%, 20%, one third or 50%, or a filer who now says it may make proposals to management.</p></div>
<div class="ind"><h3>Trades with their disclosed prices</h3><p class="small muted">5% reports list the holder's trades of the previous 60 days. We return each row with its date, venue, shares and any unit price stated, netted per holder.</p></div>
<div class="ind"><h3>Both sides of a cross-holding</h3><p class="small muted">Strategic holdings are matched to tickers, and a mutual pair is marked confirmed only when both companies' reports show it.</p></div>
<div class="ind"><h3>Foreign holders, mapped to Tokyo tickers</h3><p class="small muted">U.S. fund holdings from SEC Form N-PORT are matched to the same ticker where we can confirm the match, so one identifier can reach both. N-PORT data is quarterly and published with a delay.</p></div>
</div></div></section>

<section><div class="wrap feature">
<div class="stack"><span class="eyebrow">Ownership changes</span><h2>See the day an investor crosses a line.</h2>
<p class="muted">Under Japan's large-shareholding rules, holders above 5% of a listed company file a report, and file again after changes the filings describe. Check the rules and the filing itself for exact thresholds and deadlines. Each filing becomes an event with a one-line English summary; the event type and summary are our own wording.</p>
<ul><li>New stakes, increases, decreases and exits below 5%</li><li>Crossings of 10%, 20%, one third and 50%</li><li>Filers who newly state they may make proposals to management</li><li>Webhooks signed with HMAC, on every plan</li></ul></div>
${sampleJson ? `<div class="stack" style="gap:8px;min-width:0"><span class="eyebrow">GET /v1/events?type=${esc(sample.type)}&amp;limit=1</span><pre><code>${esc(JSON.stringify(sampleJson, null, 2))}</code></pre></div>` : ""}
</div></section>

${flows.length ? `<section><div class="wrap feature">
<div class="stack"><span class="eyebrow">Disclosed trades</span><h2>What 5% holders say they traded.</h2>
<p class="muted">These are the trade tables inside 5% reports, as filed. Unit prices are usually stated only for off-market trades. Stock lending, custody transfers and option exercises are flagged and left out of net shares.</p>
<p><a href="/companies/${esc(flows[0].ticker)}">See this on the company page →</a></p></div>
<div class="panel"><div class="section-head" style="padding:14px 16px 0;margin:0"><span class="eyebrow">Ticker ${esc(flows[0].ticker)} · from 5% reports</span></div>
<div class="scroll"><table><tr><th>Holder</th><th>Period</th><th class="num">Net shares</th><th class="num">Avg. filed price</th></tr>
${flows.map((h: any) => `<tr><td>${esc(holderName(h.holder))}</td><td class="date">${esc(h.first_trade_date)} – ${esc(h.last_trade_date)}</td><td class="num">${h.net_shares > 0 ? "+" : ""}${fmtNum(h.net_shares)}</td><td class="num">¥${fmtNum(h.disclosed_vwap_jpy, 2)}</td></tr>`).join("")}
</table></div><p class="faint xs" style="padding:0 16px 14px">Net shares and the average are our calculations from the filed trade tables. Prices are unit prices stated in the filings, usually only for off-market trades. They are filed prices, not market prices or valuations.</p></div>
</div></section>` : ""}

<section><div class="wrap feature">
<div class="stack"><span class="eyebrow">Cross-shareholdings</span><h2>The web of strategic stakes, mapped.</h2>
<p class="muted">Annual reports list each strategic shareholding with its shares, book value and purpose, and whether the other side holds shares back. ${fmtNum(stats.crossHoldings)} holdings are matched to tickers, so you can follow who is unwinding.</p>
<p><a href="/cross-holdings">Largest sales in the latest fiscal year →</a></p></div>
${holds.length ? `<div class="panel"><div class="section-head" style="padding:14px 16px 0;margin:0"><span class="eyebrow">Toyota Motor · largest strategic holdings</span><a class="small" href="/companies/7203">Company page →</a></div>
<div class="scroll"><table><tr><th>Holds</th><th class="num">Book value</th><th class="num">Shares vs. prior year</th><th>Mutual</th></tr>
${holds.map((h: any) => `<tr><td>${companyLink(h.ticker, h.name_en, h.name_ja)}</td><td class="num">${fmtJpy(h.book_value_jpy, s)}</td><td class="num">${h.shares_change_pct == null ? "—" : `${h.shares_change_pct > 0 ? "+" : ""}${h.shares_change_pct.toFixed(1)}%`}</td><td>${h.reciprocal_confirmed ? `<span class="chip accent">both reports</span>` : h.held_back_as_stated ? "stated" : "—"}</td></tr>`).join("")}
</table></div></div>` : ""}
</div></section>

${topFunds.length ? `<section><div class="wrap feature">
<div class="stack"><span class="eyebrow">U.S. funds</span><h2>Which U.S. funds hold a Tokyo stock.</h2>
<p class="muted">${fmtNum(funds.fund_count)} U.S. mutual funds and ETFs reported ${fmtNum(funds.total_shares)} Toyota shares worth ${fmtUsd(funds.total_value_usd)} in their latest N-PORT filings of ${esc(funds.quarter)}. Values are in U.S. dollars as filed.</p></div>
<div class="panel scroll"><table><tr><th>Fund</th><th class="num">Value</th><th>As of</th></tr>
${topFunds.map((f: any) => `<tr><td>${esc(f.fund)}</td><td class="num">${fmtUsd(f.value_usd)}</td><td class="date">${esc(f.report_date)}</td></tr>`).join("")}</table></div>
</div></section>` : ""}

${brief && (lr?.summary_en || (rl && !rl.data_warning)) ? `<section><div class="wrap feature">
<div class="stack"><span class="eyebrow">Company brief</span><h2>One company from its filings, in one call.</h2>
<p class="muted">One call returns a company's latest period results, indicator figures, ownership changes and an interest example on its reported debt, each with its date. It reports figures and adds no forecast or evaluation of its own.</p>
<p><a href="/docs#indicators">API reference →</a></p></div>
<div class="stack" style="gap:10px;min-width:0">
<dl class="kv" style="margin:0;grid-template-columns:repeat(2,minmax(0,1fr))">
${lr?.summary_en ? `<div style="grid-column:1/-1"><dt>Toyota: latest results${lr.release_date ? `, ${esc(lr.release_date)}` : ""}</dt><dd class="small">${esc(lr.summary_en)}</dd></div>` : ""}
${rl && !rl.data_warning ? `<div><dt>Toyota: ${esc(rl.shock_bp)} bp on all debt</dt><dd>${fmtJpyAuto(rl.extra_annual_interest_jpy)}</dd></div>` : ""}
</dl>
<p class="faint xs">The debt line is our own arithmetic on reported debt, not a forecast.</p>
</div></div></section>` : ""}

<section><div class="wrap feature">
<div class="stack"><span class="eyebrow">For AI agents</span><h2>Give your agent the filings.</h2>
<p class="muted">A remote MCP server with ${MCP_TOOLS.length} tools covers the same data as the API, with the same key. Records carry a filing ID or a link to their source, so an agent can cite what it read.</p>
<div class="panel"><table>
<tr><th>Ask your agent</th><th>Tool it can call</th></tr>
<tr><td class="small">Who newly said they may make proposals this month?</td><td><code>get_ownership_events</code></td></tr>
<tr><td class="small">Which stakes does Silchester report in Japan?</td><td><code>get_investor</code></td></tr>
<tr><td class="small">Give me a one-page brief on Toyota from its filings.</td><td><code>get_company_brief</code></td></tr>
</table></div></div>
<div class="stack" style="gap:8px;min-width:0"><pre><code># Claude Code
claude mcp add --transport http japanfilings ${o}/mcp \\
  --header "Authorization: Bearer $JAPANFILINGS_KEY"

# REST
curl -H "Authorization: Bearer $JAPANFILINGS_KEY" \\
  "${o}/v1/events?type=stated_may_make_proposals&amp;limit=5"</code></pre>
<p class="small"><a href="/docs#mcp">Setup for Claude Desktop and Cursor →</a></p></div>
</div></section>

<section><div class="wrap feature">
<div class="stack"><span class="eyebrow">What is not here</span><h2>Know the limits before you build.</h2>
<ul><li>No share prices, and no investment advice. Join your own source with the FIGI and, where our mapping covers it, the ticker symbols on each company record.</li>
<li>No TDnet feed and no earnings releases. Results between annual reports come from half-year and quarterly securities reports on EDINET, which arrive weeks after the period ends.</li>
<li>5% filings on file start ${esc(stats.earliest ?? "recently")}, and annual reports start June 2026, though each report carries five years of headline figures.</li>
<li>Most text stays in Japanese: purposes, shareholder names and quoted sentences are served as filed.</li></ul></div>
<div class="stack"><span class="eyebrow">Sources</span><p class="muted">EDINET (Financial Services Agency of Japan), SEC Form N-PORT data sets, and OpenFIGI.</p><p><a href="/sources">Sources and licences →</a></p></div>
</div></section>

<section><div class="wrap stack" style="gap:20px">
<div class="section-head" style="margin:0"><h2>Pricing</h2></div>
${extras.billingOpen ? OPEN_NOTE : BILLING_NOTE}
${pricingCards(extras.billingOpen, user)}
<p class="faint xs">5% filings are current to ${esc(stats.latest ?? "—")}. Data may be incomplete, delayed or wrong; check the original filing. Not investment advice.</p>
</div></section>`, user, { path: "/", description: "Japanese listed-company filings as English JSON for developers and AI agents: 5% stakes, stated proposal intentions, disclosed trades, cross-shareholdings, financials. REST, MCP, webhooks." });
}

// ---------- Data pages ----------

export function eventsPage(user: User | null, events: any[], type: string, freeLimited: string | null) {
  const types = Object.keys(EVENT_LABEL);
  return page("Ownership changes in Japanese listed companies", `<div class="wrap">
<div class="co-head"><span class="eyebrow">From 5% rule filings</span><h1>Ownership changes</h1>
<p class="lead">New stakes, increases, exits, threshold crossings and filers stating they may make proposals at Tokyo-listed companies over the last 30 days, newest first. Each line links to the original filing.</p>
<nav class="row" aria-label="Filter by type"><a class="chip${type ? "" : " accent"}" href="/events">All</a>${types.map((t) => `<a class="chip${t === type ? " accent" : ""}" href="/events?type=${t}">${EVENT_LABEL[t]}</a>`).join("")}</nav></div>
<div class="panel">${eventList(events)}</div>
<p class="faint xs" style="margin-top:12px">${freeLimited ? `Showing filings since ${esc(freeLimited)}. ` : ""}The API returns the last 90 days on Starter and everything on file on Pro, with filters by investor and webhooks. <a href="/docs#events">API reference</a>.</p>
</div>`, user, { path: "/events", description: "Latest 5% stake changes, threshold crossings and stated proposal intentions at Japanese listed companies, in English." });
}

export function crossHoldingsPage(user: User | null, rows: any[]) {
  const s = scaleFor(rows.map((r) => r.listed_sale_amount_jpy));
  const c = scaleFor(rows.map((r) => r.listed_carrying_amount_jpy));
  return page("Cross-shareholding unwinding in Japan", `<div class="wrap">
<div class="co-head"><span class="eyebrow">Public sample · from annual securities reports</span><h1>Largest stated sales of cross-shareholdings</h1>
<p class="lead">Largest stated sales of listed strategic shareholdings in the latest fiscal year, as reported in the "status of shareholdings" section of the annual report.</p></div>
<div class="panel scroll"><table><tr><th>Company</th><th class="num">Sold in year</th><th class="num">Issues reduced</th><th class="num">Still held</th><th class="num">Listed issues</th><th>Year end</th></tr>
${rows.map((r) => `<tr><td>${companyLink(r.ticker, r.name_en, null)}</td><td class="num">${fmtJpy(r.listed_sale_amount_jpy, s)}</td><td class="num">${fmtNum(r.listed_issues_decreased)}</td><td class="num">${fmtJpy(r.listed_carrying_amount_jpy, c)}</td><td class="num">${fmtNum(r.listed_issues)}</td><td class="date">${esc(r.fiscal_year_end)}</td></tr>`).join("")}
</table></div>
<p class="faint xs" style="margin-top:12px">This unauthenticated page is a public sample of the Pro dataset. Sale amounts are as reported by each company for listed shares held for purposes other than pure investment. Reports whose amounts exceed the company's total assets (unit-tagging errors in the filing) are left out. Full holdings, mutual pairs and purposes per company are in the API.</p>
</div>`, user, { path: "/cross-holdings", description: "Which Japanese companies sold the most cross-shareholdings this year, from annual securities reports." });
}

export function searchPage(user: User | null, q: string, results: any[]) {
  return page(q ? `Search: ${q}` : "Search companies", `<div class="wrap">
<div class="co-head"><h1>Search companies</h1>
<form class="field" action="/search" method="get"><input name="q" id="q-main" type="search" value="${esc(q)}" placeholder="Ticker or name, e.g. 7203, Toyota, トヨタ" aria-label="Ticker or company name"><button class="btn" type="submit">Search</button></form></div>
${q ? (results.length ? `<div class="panel scroll"><table><tr><th>Ticker</th><th>Company</th><th>Sector</th><th>Year end</th></tr>
${results.map((r) => `<tr><td class="mono">${esc(r.ticker)}</td><td><a href="/companies/${esc(r.ticker)}">${esc(r.name_en ?? r.name_ja)}</a><div class="ja">${esc(r.name_ja)}</div></td><td class="small">${esc(r.industry_en ?? r.industry_ja ?? "—")}</td><td class="small">${esc(r.fiscal_year_end_en ?? "—")}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">No listed company matches “${esc(q)}”. Try the 4-character ticker or part of the English or Japanese name.</p>`) : ""}
</div>`, user, { noindex: true });
}

/** "シルチェスター… (Silchester International Investors LLP)" -> the English part when the filing gives one. */
const holderName = (h: string | null) => (h ? h.match(/\(([A-Za-z][^()]*)\)\s*$/)?.[1] ?? h : "—");

/** Disclosed unit prices over time: one dot per priced trade, acquisitions filled, disposals hollow. */
function priceDots(rows: any[]) {
  const pts = rows.filter((r) => r.kind === "trade" && r.common && isNum(r.unit_price_jpy) && !Number.isNaN(Date.parse(r.trade_date))).slice(0, 400);
  if (pts.length < 2) return "";
  const t = (d: string) => Date.parse(d);
  const xs = pts.map((p) => t(p.trade_date)), ys = pts.map((p) => p.unit_price_jpy);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)], [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const W = 640, H = 180, L = 56, R = 12, T = 12, B = 26;
  const X = (v: number) => L + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * (W - L - R);
  const pad = (y1 - y0) * 0.08 || y0 * 0.05 || 1;
  const Y = (v: number) => T + (1 - (v - (y0 - pad)) / (y1 - y0 + 2 * pad)) * (H - T - B);
  const ticks = [y0, (y0 + y1) / 2, y1];
  const dots = pts.map((p) => {
    const buy = p.side === "acquisition";
    return `<circle cx="${X(t(p.trade_date)).toFixed(1)}" cy="${Y(p.unit_price_jpy).toFixed(1)}" r="4.5" fill="${buy ? "var(--accent)" : "var(--bg)"}" stroke="${buy ? "var(--bg)" : "var(--muted)"}" stroke-width="${buy ? 1.5 : 2}"><title>${esc(p.trade_date)} · ${buy ? "bought" : "sold"} ${fmtNum(p.shares)} @ ¥${fmtNum(p.unit_price_jpy, 2)} · ${esc(holderName(p.holder))}</title></circle>`;
  }).join("");
  return `<figure class="chart" style="margin:0"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Unit prices disclosed in 5% reports">
${ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--line)"/><text x="${L - 8}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end" class="axis">¥${fmtNum(v)}</text>`).join("")}
<text x="${L}" y="${H - 6}" class="axis">${new Date(x0).toISOString().slice(0, 10)}</text><text x="${W - R}" y="${H - 6}" text-anchor="end" class="axis">${new Date(x1).toISOString().slice(0, 10)}</text>
${dots}</svg>
<figcaption class="faint xs legend"><span><svg width="10" height="10"><circle cx="5" cy="5" r="4" fill="var(--accent)"/></svg> Bought</span><span><svg width="10" height="10"><circle cx="5" cy="5" r="3.5" fill="none" stroke="var(--muted)" stroke-width="1.5"/></svg> Sold</span><span>Hover a dot for the trade</span></figcaption></figure>`;
}

function tradesSection(ticker: string, tr: any) {
  if (!tr?.data?.length) return "";
  return `<section><div class="section-head"><h2>Trades by 5% holders</h2><span class="faint small">From the 60-day trade tables in 5% reports</span></div>
<div class="stack" style="gap:16px">
${priceDots(tr.data)}
<div class="panel scroll"><table><tr><th>Holder</th><th>Period</th><th class="num">Net shares</th><th class="num">Net % of shares</th><th class="num">Avg. filed price</th><th class="num">Range</th></tr>
${tr.by_holder.slice(0, 12).map((h: any) => `<tr><td>${h.filer_edinet_code ? `<a href="/investors/${esc(h.filer_edinet_code)}">${esc(holderName(h.holder))}</a>` : esc(holderName(h.holder))}</td><td class="date">${esc(h.first_trade_date)} – ${esc(h.last_trade_date)}</td><td class="num">${h.net_shares > 0 ? "+" : ""}${fmtNum(h.net_shares)}</td><td class="num">${fmtPt(h.net_pct_of_outstanding)}</td><td class="num">${isNum(h.disclosed_vwap_jpy) ? "¥" + fmtNum(h.disclosed_vwap_jpy, 2) : "—"}</td><td class="num">${isNum(h.disclosed_low_jpy) ? `¥${fmtNum(h.disclosed_low_jpy)}–${fmtNum(h.disclosed_high_jpy)}` : "—"}</td></tr>`).join("")}
</table></div>
<p class="faint xs">Net shares and the average are our calculations. Prices are filed prices as stated by the filer, mostly for off-market trades; they are not market prices. Stock lending, custody transfers and option exercises are excluded from net shares. Every row via <code>/v1/companies/${esc(ticker)}/trades</code>.</p>
</div></section>`;
}

const AREA_LABEL: Record<string, string> = { trade: "Trade & tariffs", fiscal_tax: "Fiscal & tax", monetary: "Monetary", regulation: "Regulation", politics: "Politics & geopolitics" };
const METRIC_LABEL: Record<string, string> = { roe: "ROE, %", ordinary_margin: "Ordinary margin", equity_ratio: "Equity ratio, %", revenue_growth: "Revenue growth", net_income_growth: "Net income growth", ocf_to_assets: "Operating cash flow / assets" };

function filingsFactsSection(ticker: string, more: any) {
  const stated = Object.entries(more?.ratings?.latest_stated ?? {}) as [string, any][];
  const res = more?.buyback?.resolutions?.[0], gov = more?.governance ?? [];
  if (!stated.length && !res && !gov.length) return "";
  return `<section><div class="section-head"><h2>From other filings</h2><span class="faint small">As the company states them</span></div><div class="stack" style="gap:12px">
${stated.length ? `<div class="small"><strong>Bond ratings stated in filings:</strong> ${stated.map(([a, r]) => `${esc(a)} ${esc(r.rating)} (${esc(r.rating_date ?? r.submit_date)}${r.bond_ja ? `, <span class="ja">${esc(r.bond_ja)}</span>` : ""})`).join("; ")}. One bond's rating on that date, as the company stated it; it may have changed. The agencies' opinions, not ours. <code>/v1/companies/${esc(ticker)}/credit-ratings</code></div>` : ""}
${res ? `<div class="small"><strong>Share buyback, report to ${esc(more.buyback.period_end ?? "—")}:</strong> ${isNum(res.cumulative_acquired_yen) ? `${fmtJpyAuto(res.cumulative_acquired_yen)} acquired` : "acquired amount not stated"}${isNum(res.authorized_max_yen) ? ` of a ${fmtJpyAuto(res.authorized_max_yen)} limit` : ""}. <code>/v1/companies/${esc(ticker)}/buybacks</code></div>` : ""}
${gov.map((g: any) => `<div class="small"><strong>${g.type === "auditor_change" ? "Auditor change" : "Internal control"}, ${esc(g.submit_date)}:</strong> ${esc(g.conclusion ?? (g.incoming_auditor ? `new auditor ${g.incoming_auditor}` : "see filing"))}${g.quote_ja ? ` <span class="ja muted">${esc(g.quote_ja)}</span>` : ""} <a href="${esc(g.source_url)}" rel="noopener">Filing</a></div>`).join("")}
</div></section>`;
}

function newDisclosureSection(ticker: string, more: any) {
  const segments = more?.segments?.segments ?? [], voting = more?.voting ?? [], offers = more?.tenderOffers ?? [];
  if (!segments.length && !voting.length && !offers.length) return "";
  return `<section><div class="section-head"><h2>Segments, votes and tender offers</h2><span class="faint small">Stated in EDINET filings</span></div><div class="stack" style="gap:12px">
${segments.length ? `<div class="small"><strong>Segments:</strong> ${segments.slice(0, 8).map((s: any) => esc(s.name_en ?? s.name_ja ?? "Unnamed segment")).join(", ")}. <code>/v1/companies/${esc(ticker)}/segments</code></div>` : ""}
${voting.length ? `<div class="small"><strong>Latest voting result:</strong> ${esc(voting[0].meeting_date ?? voting[0].submit_date)} · ${esc(scrubProposalTitle(voting[0].proposal_title_ja) ?? "Proposal")} · ${esc(voting[0].result ?? "result not parsed")}. <code>/v1/companies/${esc(ticker)}/voting</code></div>` : ""}
${offers.length ? `<div class="small"><strong>Latest tender-offer filing:</strong> ${esc(offers[0].submit_date ?? "—")} · ${esc(offers[0].document_kind)} · ${esc(offers[0].target_name_ja ?? "target not parsed")}. <code>/v1/companies/${esc(ticker)}/tender-offers</code></div>` : ""}
</div></section>`;
}

function indicatorsSection(ticker: string, ind: any) {
  if (!ind) return "";
  const fp = ind.financial_percentiles ?? {}, pu = ind.policy_uncertainty, oa = ind.ownership_activity ?? {}, wb = ind.results_wording_balance;
  const areas = pu ? Object.entries(pu.policy_uncertainty_by_area ?? {}).filter(([, n]) => (n as number) > 0) : [];
  const rl = ind.rate_lens;
  const rateLine = rl && !rl.data_warning ? `<p class="small" style="margin-top:10px">Interest example: if all ${fmtJpyAuto(rl.interest_bearing_debt_jpy)} of reported debt repriced by ${rl.shock_bp} basis points, a year's interest would rise by ${fmtJpyAuto(rl.extra_annual_interest_jpy)}${isNum(rl.extra_interest_pct_of_operating_cf) ? `, ${rl.extra_interest_pct_of_operating_cf.toFixed(1)}% of operating cash flow` : ""}. Our arithmetic only, not a forecast: fixed-rate debt and hedges are ignored.</p>` : "";
  const metrics = Object.entries(fp.metrics ?? {}) as [string, any][];
  return `<section><div class="section-head"><h2>Indicators</h2><span class="faint small">Computed by us from filings · fiscal year to ${esc(ind.fiscal_year_end)}</span></div>
${metrics.length ? `<div class="panel scroll"><table><tr><th>Metric</th><th class="num">Value</th><th class="num">Percentile among listed companies</th></tr>${metrics.map(([k, m]) => `<tr><td>${esc(METRIC_LABEL[k] ?? k)}</td><td class="num">${fmtNum(m.value, 4)}</td><td class="num">${fmtNum(m.percentile, 1)}</td></tr>`).join("")}</table></div>` : ""}
<div class="ind-grid" style="margin-top:12px">
<div class="ind"><span class="k">Policy-uncertainty measure</span><span class="v">${pu ? pu.policy_uncertainty_per_100.toFixed(2) : "—"}</span>
<span class="small muted">${pu ? "Share of the report's text discussing policy-related uncertainty, by policy area. Proprietary text method." : "No narrative text in the report."}</span>
${areas.length ? `<div>${areas.map(([a, n]) => `<span class="chip">${esc(AREA_LABEL[a] ?? a)} ${n}</span>`).join(" ")}</div>` : ""}</div>
<div class="ind"><span class="k">Ownership activity</span>
<span class="small muted">${fmtNum(oa.filers_stating_may_make_proposals_365d)} filer(s) stated they may make proposals in 365 days; ${fmtNum(oa.new_5pct_holders_180d)} new 5% holder(s) in 180 days; net stake change ${fmtPt(oa.net_stake_change_pt_90d)} in 90 days; ${fmtPct(oa.held_by_listed_companies_pct, 1)} held by other listed companies.</span></div>
</div>
${wb && isNum(wb.balance) ? `<p class="faint xs" style="margin-top:8px">Results wording balance: ${wb.balance > 0 ? "+" : ""}${wb.balance.toFixed(2)}. Proprietary text method; it describes the wording only and does not interpret context.</p>` : ""}
${rateLine}<p class="faint xs" style="margin-top:6px">Via <code>/v1/companies/${esc(ticker)}/indicators</code>. Descriptive figures, not a rating and not investment advice.</p></section>`;
}

const REL_FIELDS: [string, string][] = [["revenue", "Revenue"], ["adjusted_operating_income", "Adjusted operating profit"], ["operating_income", "Operating profit"], ["ordinary_income", "Ordinary profit"], ["profit_before_tax", "Profit before tax"], ["net_income_parent", "Net profit to owners"]];

function releaseSection(rel: any) {
  if (!rel?.results) return "";
  const rows = REL_FIELDS.filter(([k]) => isNum(rel.results[k + "_mn_jpy"]));
  const mn = (v: unknown) => (isNum(v) ? fmtJpyAuto(v * 1e6) : "—");
  const yoy = (v: unknown) => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(1)}%` : "—");
  return `<section class="first"><div class="section-head"><h2>${rel.period === "H1" ? "Latest half-year results" : "Latest period results"}</h2><span class="faint small">${esc(rel.period === "FY" ? "Full year" : rel.period === "H1" ? "First half" : rel.period + " cumulative")} · fiscal year to ${esc(rel.fiscal_year_end_month)} · filed ${esc(rel.release_date ?? "—")}</span></div>
${rel.summary_en ? `<p class="small" style="margin-bottom:10px">${esc(rel.summary_en)}</p>` : ""}<div class="panel scroll"><table><tr><th></th><th class="num">This period</th><th class="num">Year on year</th></tr>
${rows.map(([k, label]) => `<tr><td>${label}</td><td class="num">${mn(rel.results[k + "_mn_jpy"])}</td><td class="num">${yoy(rel.results[k + "_yoy_pct"])}</td></tr>`).join("")}
</table></div>
<p class="faint xs" style="margin-top:8px">From the securities report on <a href="${esc(rel.source_url)}" rel="noopener">EDINET</a>, which states no forecast. History via <code>/v1/companies/${esc(rel.ticker)}/earnings-releases</code>.</p></section>`;
}

/** Monthly policy-uncertainty index: one bar per filing month, bar width fixed, months with few reports drawn faint. */
function monthlyIndexChart(rows: any[]) {
  if (!rows.length) return "";
  const W = 640, H = 200, L = 40, R = 8, T = 14, B = 30;
  const max = Math.max(...rows.map((r) => r.mean_policy_uncertainty), 0.01);
  const bw = (W - L - R) / rows.length;
  const Y = (v: number) => T + (1 - v / max) * (H - T - B);
  return `<figure class="chart" style="margin:0"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Policy-uncertainty measure by filing month">
${[0, max / 2, max].map((v) => `<line x1="${L}" x2="${W - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" class="base"/><text x="${L - 6}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${v.toFixed(2)}</text>`).join("")}
${rows.map((r, i) => {
    const x = L + i * bw + 2, h = Y(0) - Y(r.mean_policy_uncertainty);
    return `<rect x="${x.toFixed(1)}" y="${Y(r.mean_policy_uncertainty).toFixed(1)}" width="${Math.max(2, bw - 4).toFixed(1)}" height="${h.toFixed(1)}" rx="2" class="bar" opacity="${r.reports < 30 ? 0.35 : 1}"><title>${esc(r.month)}: measure ${r.mean_policy_uncertainty.toFixed(2)}, ${r.reports} reports, ${(100 * r.share_of_reports_mentioning).toFixed(0)}% discuss policy uncertainty</title></rect>` +
      (i % Math.ceil(rows.length / 8) === 0 ? `<text x="${(x + (bw - 4) / 2).toFixed(1)}" y="${H - 10}" text-anchor="middle">${esc(r.month.slice(2))}</text>` : "");
  }).join("")}
</svg><figcaption class="faint xs legend"><span>Bar height: mean policy-uncertainty measure</span><span>Faint bars: fewer than 30 reports that month</span></figcaption></figure>`;
}

export function indicatorsPage(user: User | null, index: any[], roe: any[], uncertainty: any[], newHolders: any[]) {
  const latest = [...index].reverse().find((r) => r.reports >= 30);
  const list = (title: string, rows: any[], val: (r: any) => string, sub: string) => `<div class="stack" style="gap:8px;min-width:0"><h3>${title}</h3><p class="faint xs">${sub}</p>
<div class="panel scroll"><table><tr><th>Company</th><th class="num">Value</th></tr>${rows.map((r) => `<tr><td>${companyLink(r.ticker, r.name_en ?? null, r.name_ja)}</td><td class="num">${val(r)}</td></tr>`).join("")}</table></div></div>`;
  return page("Indicators from Japanese filings", `<div class="wrap">
<div class="co-head"><span class="eyebrow">Public sample · computed from EDINET filings</span><h1>Indicators</h1>
<p class="lead">Descriptive figures for listed companies with an annual report on file: each financial metric with its percentile rank among listed companies, how much of the report discusses economic-policy uncertainty, and counts of ownership activity from filings. None of them is a rating.</p></div>
<section class="first"><div class="section-head"><h2>Japan corporate policy-uncertainty index</h2><span class="faint small">Annual securities reports by filing month</span></div>
${latest ? `<p class="small">${esc(latest.month)}: ${(100 * latest.share_of_reports_mentioning).toFixed(0)}% of ${fmtNum(latest.reports)} reports discussed economic-policy uncertainty. By area: ${Object.entries(latest.share_by_area).sort((a: any, b: any) => b[1] - a[1]).map(([a, v]: any) => `${esc(AREA_LABEL[a] ?? a)} ${(100 * v).toFixed(0)}%`).join(", ")}.</p>` : ""}
${monthlyIndexChart(index)}
<p class="faint xs" style="margin-top:8px">A descriptive view of how annual reports discuss economic-policy uncertainty. Monthly series via <code>/v1/indicators/policy-uncertainty</code>.</p></section>
<section><div class="grid-3" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:20px">
${list("Sorted by ROE", roe, (r) => fmtNum(r.value, 1), "As stated in the latest annual report, %.")}
${list("Sorted by policy-uncertainty measure", uncertainty, (r) => fmtNum(r.value, 2), "Relative frequency in the latest annual report.")}
${list("Sorted by new 5% holders", newHolders, (r) => fmtNum(r.value), "Filers with a first 5% report in the last 180 days.")}
</div><p class="faint xs" style="margin-top:12px">This page is an unauthenticated public sample. A list in the order of one figure says nothing about which company is better. Any metric, with a sector filter, via <code>/v1/indicators/sorted</code>. Not investment advice.</p></section>
</div>`, user, { path: "/indicators", description: "Per-metric percentiles, policy-uncertainty counts and ownership activity for Japanese listed companies, computed from EDINET filings." });
}

export function companyPage(user: User | null, c: any, fin: any, det: any, holdings: any[], funds: any = null, xh: any = null, events: any[] = [], tr: any = null, ind: any = null, rel: any = null, more: any = null) {
  // A few filers register their English name in lower case; show those in title case.
  const name = c.name_en && c.name_en === c.name_en.toLowerCase() ? c.name_en.replace(/\b[a-z]/g, (m: string) => m.toUpperCase()) : c.name_en || c.name_ja;
  const km = ((fin?.key_metrics ?? []) as any[]).slice().reverse(); // oldest first for charts
  const latest = fin?.key_metrics?.[0];
  const fy = (d: string) => (d ? `FY${d.slice(0, 4)}` : "—"); // fiscal year named by the year it ends
  const ts = scaleFor((fin?.key_metrics ?? []).flatMap((r: any) => [r.revenue, r.net_income, r.total_assets, r.net_assets, r.operating_cf]));

  const overview = latest ? `<section${rel?.results ? "" : ` class="first"`}><div class="stack" style="gap:20px">
<div class="section-head" style="margin:0"><h2>Financials</h2><span class="faint small">${esc(fin.accounting_standard)} · ${esc(String(latest.basis ?? "").replace("_", "-"))} · fiscal year to ${esc(latest.fiscal_year_end)}</span></div>
<dl class="kv" style="margin:0">
<div><dt>Revenue</dt><dd>${fmtJpyAuto(latest.revenue)}</dd></div>
<div><dt>Net income</dt><dd>${fmtJpyAuto(latest.net_income)}</dd></div>
<div><dt>ROE</dt><dd>${fmtPct(latest.roe, 1)}</dd></div>
<div><dt>Equity ratio</dt><dd>${fmtPct(latest.equity_ratio, 1)}</dd></div>
<div><dt>Dividend / share</dt><dd>${isNum(latest.dividend_per_share) ? `¥${fmtNum(latest.dividend_per_share, 2)}` : "—"}</dd></div>
<div><dt>Employees</dt><dd>${fmtNum(latest.employees)}</dd></div>
</dl>
<div class="charts">${barChart("Revenue", km.map((r) => ({ label: fy(r.fiscal_year_end), value: r.revenue })))}${barChart("Net income", km.map((r) => ({ label: fy(r.fiscal_year_end), value: r.net_income })))}</div>
<div class="panel scroll"><table><tr><th>Fiscal year</th><th class="num">Revenue</th><th class="num">Net income</th><th class="num">Total assets</th><th class="num">Net assets</th><th class="num">ROE</th><th class="num">EPS</th><th class="num">Operating CF</th></tr>
${(fin.key_metrics as any[]).map((r) => `<tr><td class="date">${esc(r.fiscal_year_end)}</td><td class="num">${fmtJpy(r.revenue, ts)}</td><td class="num">${fmtJpy(r.net_income, ts)}</td><td class="num">${fmtJpy(r.total_assets, ts)}</td><td class="num">${fmtJpy(r.net_assets, ts)}</td><td class="num">${fmtPct(r.roe, 1)}</td><td class="num">${isNum(r.eps) ? `¥${fmtNum(r.eps, 2)}` : "—"}</td><td class="num">${fmtJpy(r.operating_cf, ts)}</td></tr>`).join("")}
</table></div></div></section>`
    : `<section${rel?.results ? "" : ` class="first"`}><p class="muted">Financial statements appear when the company's next annual securities report is processed.</p></section>`;

  const changes = events.length ? `<section><div class="section-head"><h2>Ownership changes</h2><a class="small" href="/events">All companies →</a></div>
<div class="panel">${eventList(events, { showCompany: false })}</div></section>` : "";

  const stakes = holdings.length ? `<section><div class="section-head"><h2>5% filings</h2><span class="faint small">Large shareholding reports on ${esc(c.ticker)}</span></div>
<div class="panel scroll"><table><tr><th>Filed</th><th>Filer</th><th class="num">Stake</th><th class="num">Change</th><th>Stated purpose</th></tr>
${holdings.map((h) => `<tr><td class="date">${esc(h.submit_date)}</td><td>${h.filer_edinet_code ? `<a href="/investors/${esc(h.filer_edinet_code)}">${esc(h.filer_name_en ?? h.filer_name_en_filing ?? h.filer_name_ja)}</a>` : esc(h.filer_name_en ?? h.filer_name_en_filing ?? h.filer_name_ja)}</td><td class="num">${fmtPct(h.holding_ratio_pct)}</td><td class="num">${fmtPt(h.ratio_change_pt)}</td><td>${(h.purpose_tags ?? []).map((t: string) => `<span class="chip">${esc(t.replace(/_/g, " "))}</span>`).join(" ") || "—"}</td></tr>`).join("")}
</table></div></section>` : "";

  const sm = xh?.summary;
  const xs = scaleFor((xh?.holds ?? []).map((h: any) => h.book_value_jpy));
  const cross = xh ? `<section><div class="section-head"><h2>Cross-shareholdings</h2><span class="faint small">From the annual report's status of shareholdings</span></div>
${sm && !sm.data_warning ? `<dl class="kv" style="margin:0 0 16px">
<div><dt>Listed strategic holdings</dt><dd>${fmtNum(sm.listed_issues)} <small>issues</small></dd></div>
<div><dt>Book value</dt><dd>${fmtJpyAuto(sm.listed_carrying_amount_jpy)}</dd></div>
<div><dt>Sold in the year</dt><dd>${fmtJpyAuto(sm.listed_sale_amount_jpy)} <small>${fmtNum(sm.listed_issues_decreased)} issues</small></dd></div>
<div><dt>Bought in the year</dt><dd>${fmtJpyAuto(sm.listed_acquisition_cost_jpy)} <small>${fmtNum(sm.listed_issues_increased)} issues</small></dd></div>
<div><dt>Mutual, both reports</dt><dd>${fmtNum(sm.mutual_confirmed_both_reports)}</dd></div>
<div><dt>Held by listed cos.</dt><dd>${fmtNum(sm.held_by_listed_companies)}</dd></div>
</dl>` : sm?.data_warning ? `<p class="notice warn small" style="margin-bottom:16px">${esc(sm.data_warning)}</p>` : ""}
${xh.holds.length ? `<div class="panel scroll"><table><tr><th>Holds</th><th class="num">Book value</th><th class="num">Shares vs. prior year</th><th>Mutual</th></tr>
${xh.holds.slice(0, 10).map((h: any) => `<tr><td>${companyLink(h.ticker, h.name_en, h.name_ja)}</td><td class="num">${fmtJpy(h.book_value_jpy, xs)}</td><td class="num">${h.shares == null ? "sold all" : h.shares_change_pct == null ? "—" : `${h.shares_change_pct > 0 ? "+" : ""}${h.shares_change_pct.toFixed(1)}%`}</td><td>${h.reciprocal_confirmed ? `<span class="chip accent">both reports</span>` : h.held_back_as_stated ? "stated" : h.held_back_as_stated === false ? "no" : "—"}</td></tr>`).join("")}
</table></div>` : ""}
${xh.held_by.length ? `<p class="small" style="margin-top:14px"><span class="muted">Held by:</span> ${xh.held_by.slice(0, 12).map((h: any) => companyLink(h.holder_ticker, h.holder_name_en, h.holder_name_ja)).join(", ")}${xh.held_by.length > 12 ? ` <span class="muted">and ${xh.held_by.length - 12} more</span>` : ""}</p>` : ""}
<p class="faint xs" style="margin-top:10px">Purposes, all holdings and mutual flags via <code>/v1/companies/${esc(c.ticker)}/cross-holdings</code>. Share changes include stock splits.</p></section>` : "";

  const holders = det?.major_shareholders?.length ? `<section><div class="section-head"><h2>Major shareholders</h2><span class="faint small">As of fiscal year end ${esc(det.fiscal_year_end)}</span></div>
<div class="panel scroll"><table><tr><th>Shareholder</th><th class="num">Shares</th><th class="num">Ratio</th></tr>
${det.major_shareholders.map((s: any) => `<tr><td>${esc(s.name)}</td><td class="num">${fmtNum(s.shares)}</td><td class="num">${fmtPct(s.ratio_pct)}</td></tr>`).join("")}</table></div></section>` : "";

  const usFunds = funds?.top_funds?.length ? `<section><div class="section-head"><h2>U.S. funds holding ${esc(c.ticker)}</h2><span class="faint small">SEC Form N-PORT, reports to ${esc(funds.latest_report_date)}</span></div>
<p class="muted small" style="margin-bottom:14px">${fmtNum(funds.fund_count)} U.S. funds held ${fmtNum(funds.total_shares)} shares worth ${fmtUsd(funds.total_value_usd)}.</p>
<div class="panel scroll"><table><tr><th>Fund</th><th class="num">Shares</th><th class="num">Value</th><th class="num">% of fund</th><th>As of</th></tr>
${funds.top_funds.slice(0, 5).map((f: any) => `<tr><td>${esc(f.fund)}<div class="ja">${esc(f.registrant)}</div></td><td class="num">${fmtNum(f.shares)}</td><td class="num">${fmtUsd(f.value_usd)}</td><td class="num">${isNum(f.pct_of_fund) ? f.pct_of_fund.toFixed(3) + "%" : "—"}</td><td class="date">${esc(f.report_date)}</td></tr>`).join("")}</table></div>
<p class="faint xs" style="margin-top:10px">Up to 25 funds per stock via the API.</p></section>` : "";

  return page(`${name} (${c.ticker}): ownership, financials, shareholders`, `<div class="wrap">
<div class="co-head">
<span class="eyebrow">${esc(c.industry_en ?? c.industry_ja ?? "Listed company")} · Fiscal year end ${esc(c.fiscal_year_end_en ?? c.fiscal_year_end ?? "—")}</span>
<div class="co-title"><h1>${esc(name)}</h1><span class="tk">${esc(c.ticker)}</span></div>
<p class="ja" style="font-size:15px">${esc(c.name_ja)}</p>
<div class="ids"><span class="chip">FIGI ${esc(c.figi ?? "—")}</span><span class="chip">${esc(c.yahoo_symbol)}</span><span class="chip">${esc(c.bloomberg_ticker)}</span>${c.corporate_number ? `<span class="chip">Corp. no. ${esc(c.corporate_number)}</span>` : ""}</div>
</div>
${releaseSection(rel)}${overview}${indicatorsSection(c.ticker, ind)}${filingsFactsSection(c.ticker, more)}${newDisclosureSection(c.ticker, more)}${changes}${tradesSection(c.ticker, tr)}${stakes}${cross}${holders}${usFunds}
<section><div class="stack" style="gap:10px"><h3>Get this as JSON</h3>
<pre><code>GET /v1/companies/${esc(c.ticker)}
GET /v1/companies/${esc(c.ticker)}/financials
GET /v1/companies/${esc(c.ticker)}/details
GET /v1/companies/${esc(c.ticker)}/segments
GET /v1/companies/${esc(c.ticker)}/voting
GET /v1/companies/${esc(c.ticker)}/tender-offers
GET /v1/companies/${esc(c.ticker)}/indicators
GET /v1/companies/${esc(c.ticker)}/earnings-releases
GET /v1/events?ticker=${esc(c.ticker)}
GET /v1/companies/${esc(c.ticker)}/trades         
GET /v1/companies/${esc(c.ticker)}/cross-holdings 
GET /v1/companies/${esc(c.ticker)}/brief          </code></pre>
<p class="small"><a href="/pricing">Plans from $9 a month</a> · <a href="/docs">Documentation</a></p></div></section>
</div>`, user, { description: `${name} (${c.ticker}): ownership changes, 5% filings, cross-shareholdings, financial statements and major shareholders from public Japanese filings, in English.` });
}

export function investorPage(user: User | null, inv: any) {
  const name = inv.filer_name_en || inv.filer_name_ja;
  return page(`${name}: stakes in Japanese companies`, `<div class="wrap">
<div class="co-head"><span class="eyebrow">Investor · EDINET ${esc(inv.filer_edinet_code)}</span><h1>${esc(name)}</h1><p class="ja" style="font-size:15px">${esc(inv.filer_name_ja)}</p>
${inv.joint_holder_names.length > 1 ? `<p class="small"><span class="muted">Files jointly with:</span> ${inv.joint_holder_names.map((n: string) => `<span class="chip">${esc(n)}</span>`).join(" ")}</p>` : ""}</div>
<section class="first"><div class="section-head"><h2>Latest positions</h2><span class="faint small">Most recent 5% filing per company</span></div>
<div class="panel scroll"><table><tr><th>Company</th><th class="num">Stake</th><th>As of</th><th>Filing</th></tr>
${inv.latest_positions.map((p: any) => `<tr><td>${companyLink(p.ticker, p.issuer_name_en, p.issuer_name_ja)}</td><td class="num">${fmtPct(p.holding_ratio_pct)}</td><td class="date">${esc(p.as_of)}</td><td class="mono xs">${esc(p.doc_id)}</td></tr>`).join("")}</table></div></section>
${inv.filings?.length ? `<section><div class="section-head"><h2>Recent 5% filings</h2><span class="faint small">Newest first</span></div>
<div class="panel scroll"><table><tr><th>Filed</th><th>Company</th><th class="num">Stake</th><th class="num">Change</th><th>Source</th></tr>
${inv.filings.slice(0, 20).map((f: any) => `<tr><td class="date">${esc(f.submit_date)}</td><td>${companyLink(f.issuer_ticker, f.issuer_name_en, f.issuer_name_ja)}</td><td class="num">${fmtPct(f.holding_ratio_pct)}</td><td class="num">${fmtPt(f.ratio_change_pt)}</td><td class="small">${f.source_url ? `<a href="${esc(f.source_url)}" rel="noopener">PDF, Japanese</a>` : esc(f.doc_id)}</td></tr>`).join("")}</table></div>
<p class="faint xs" style="margin-top:10px">Via <code>/v1/investors/${esc(inv.filer_edinet_code)}</code>.</p></section>` : ""}
</div>`, user, { description: `${name}: stakes in Japanese listed companies from 5% rule filings.` });
}

export function investorGroupPage(user: User | null, group: any, portfolio: any) {
  const name = group.name_en || group.name_ja || group.group_id;
  const positions = portfolio?.positions ?? [];
  return page(`${name}: investor group`, `<div class="wrap"><div class="co-head"><span class="eyebrow">Investor group · ${esc(group.group_id)}</span><h1>${esc(name)}</h1><p class="muted">Members are consolidated by the stated match rule. Grouping does not mean they act together.</p></div>
<section class="first"><div class="section-head"><h2>Members</h2><span class="faint small">${fmtNum(group.members?.length ?? 0)} records</span></div><div class="panel scroll"><table><tr><th>Name</th><th>EDINET code</th><th>Match</th><th>Confidence</th></tr>${(group.members ?? []).map((m: any) => `<tr><td>${esc(m.name)}</td><td class="mono">${esc(m.edinet_code ?? "—")}</td><td>${esc(m.match_rule)}</td><td>${esc(m.confidence)}</td></tr>`).join("")}</table></div></section>
${positions.length ? `<section><div class="section-head"><h2>Reported positions</h2><span class="faint small">Latest filing per member and company</span></div><div class="panel scroll"><table><tr><th>Company</th><th class="num">Stake</th><th>As of</th><th>Status</th></tr>${positions.map((p: any) => `<tr><td>${companyLink(p.ticker, p.issuer_name_en, p.issuer_name_ja)}</td><td class="num">${fmtPct(p.last_ratio_pct)}</td><td>${esc(p.last_date)}</td><td>${esc(p.status)}</td></tr>`).join("")}</table></div></section>` : ""}</div>`, user, { description: `${name}: organization-only investor group and reported positions from public filings.` });
}

export function statusPage(user: User | null, body: any) {
  const rows = body?.data ?? [];
  return page("Data status | japanfilings", `<div class="wrap"><div class="co-head"><span class="eyebrow">Data operations</span><h1>Data status</h1><p class="lead">Freshness of each source and processed dataset.</p></div><section class="first"><div class="panel scroll"><table><tr><th>Dataset</th><th>Status</th><th>Newest source date</th><th>Last successful run</th><th class="num">Rows</th></tr>${rows.map((r: any) => `<tr><td>${esc(r.label ?? r.dataset)}</td><td>${esc(r.status)}</td><td>${esc(r.newest_source_date ?? "—")}</td><td>${esc(r.last_successful_run ?? "—")}</td><td class="num">${fmtNum(r.rows)}</td></tr>`).join("")}</table></div><p class="small" style="margin-top:12px">A dash in the last-run column means no successful run is recorded, and a dataset with no recorded run counts as late. Row counts are stored records only and do not show freshness. Machine-readable details, including the reason for each status: <code>/v1/freshness</code>.</p></section></div>`, user, { path: "/status", description: "Freshness and row counts for every japanfilings dataset." });
}

// Real responses from the API, shortened (arrays cut to one item, long strings clipped). Captured 2026-10-31.
const DOC_EXAMPLES: Record<string, string> = {
  search: "{\n  \"data\": [\n    {\n      \"ticker\": \"3116\",\n      \"name_en\": \"TOYOTA BOSHOKU CORPORATION\",\n      \"name_ja\": \"トヨタ紡織株式会社\",\n      \"industry_ja\": \"輸送用機器\",\n      \"fiscal_year_end\": \"3月31日\",\n      \"corporate_number\": \"2180301014324\",\n      \"figi\": \"BBG000BBRS83\",\n      \"industry_en\": \"Transportation Equipment\",\n      \"fiscal_year_end_en\": \"Mar 31\"\n    }\n  ]\n}",
  company: "{\n  \"edinet_code\": \"E02144\",\n  \"ticker\": \"7203\",\n  \"corporate_number\": \"1180301018771\",\n  \"name_ja\": \"トヨタ自動車株式会社\",\n  \"name_en\": \"TOYOTA MOTOR CORPORATION\",\n  \"listed\": 1,\n  \"industry_ja\": \"輸送用機器\",\n  \"fiscal_year_end\": \"3月31日\",\n  \"capital_million_jpy\": 635401,\n  \"filer_type\": \"内国法人・組合\",\n  \"figi\": \"BBG000BCM915\",\n  \"composite_figi\": \"BBG000BCM915\",\n  \"share_class_figi\": \"BBG001S6BGT4\",\n  \"industry_en\": \"Transportation Equipment\",\n  \"fiscal_year_end_en\": \"Mar 31\",\n  \"yahoo_symbol\": \"7203.T\",\n  \"bloomberg_ticker\": \"7203 JP\",\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  lh: "{\n  \"data\": [\n    {\n      \"doc_id\": \"S100Z9GY\",\n      \"report_kind\": \"change_report\",\n      \"report_title_ja\": \"変更報告書No. 5\",\n      \"filer_name_ja\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー\",\n      \"filer_edinet_code\": \"E24872\",\n      \"obligation_date\": \"2026-10-21\",\n      \"submit_date\": \"2026-10-22\",\n      \"holders_count\": 1,\n      \"change_reason_ja\": \"株券等保有割合が前回の報告書提出時より1%以上増加したこと\",\n      \"issuer_name_ja\": \"東ソー株式会社\",\n      \"issuer_ticker\": \"4042\",\n      \"exchange_ja\": \"東京証券取引所(プライム市場)\",\n      \"holding_ratio_pct\": 6.99,\n      \"previous_ratio_pct\": 5.98,\n      \"ratio_change_pt\": 1.01,\n      \"purpose_ja\": \"提出者は、発行者に対し、企業価値及び株主価値の向上を目的として、資本政策の変更、資本効率の向上、事業構造の見直し、及びコーポレート・ガバナンスの強化等に関する提案（重要提案行為等に該当し得るものを含む。）を行う方針です。これらの提案に…\",\n      \"purpose_tags\": [\n        \"may_make_proposals\"\n      ],\n      \"source_url\": \"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100Z9GY.pdf\",\n      \"source\": \"EDINET (Financial Services Agency of Japan)\",\n      \"pdf_ratio_pct\": 6.99,\n      \"ratio_source\": \"xbrl\",\n      \"issuer_name_en\": \"TOSOH CORPORATION\",\n      \"issuer_edinet_code\": \"E00767\",\n      \"issuer_corporate_number\": \"5250001010002\",\n      \"filer_name_en\": null,\n      \"holders\": [\n        {\n          \"shares\": 22724900,\n          \"ratio_pct\": 6.99,\n          \"previous_ratio_pct\": 5.98,\n          \"own_funds_jpy\": 0,\n          \"borrowings_jpy\": 0,\n          \"holder_type_ja\": \"法人(英国法に基づく有限責任事業組合(LLP))\",\n          \"name\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー (Silchester International Investors LLP)\",\n          \"purpose_ja\": \"提出者は、発行者に対し、企業価値及び株主価値の向上を目的として、資本政策の変更、資本効率の向上、事業構造の見直し、及びコーポレート・ガバナンスの強化等に関する提案(重要提案行為等に該当し得るものを含む。)を行う方針です。これらの提案に…\",\n          \"holder_type\": \"corporation\"\n        }\n      ],\n      \"filer_name_en_filing\": \"Silchester International Investors LLP\"\n    }\n  ],\n  \"history_limited_to\": null,\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  ev: "{\n  \"data\": [\n    {\n      \"type\": \"stated_may_make_proposals\",\n      \"summary_en\": \"GREEN8 Co.,Ltd. states it may make proposals to the management of Kyogoku unyu shoji Co., Ltd. (9073) (first filing).\",\n      \"doc_id\": \"S100Z968\",\n      \"submit_date\": \"2026-10-28\",\n      \"issuer_ticker\": \"9073\",\n      \"issuer_name_en\": \"Kyogoku unyu shoji Co., Ltd.\",\n      \"issuer_name_ja\": \"京極運輸商事株式会社\",\n      \"filer_edinet_code\": \"E33911\",\n      \"filer_name_en\": \"GREEN8 Co.,Ltd.\",\n      \"filer_name_ja\": \"グリーンエイト株式会社\",\n      \"holding_ratio_pct\": 5.83,\n      \"previous_ratio_pct\": null,\n      \"change_pt\": null,\n      \"purpose_tags\": [\n        \"may_make_proposals\"\n      ],\n      \"source_url\": \"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100Z968.pdf\",\n      \"event_id\": \"S100Z968:stated_may_make_proposals\",\n      \"stated_may_make_proposals\": true\n    }\n  ],\n  \"history_limited_to\": null,\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  filings: "{\n  \"data\": [\n    {\n      \"doc_id\": \"S100Z7HA\",\n      \"submit_datetime\": \"2026-10-13 11:43\",\n      \"edinet_code\": \"E02144\",\n      \"ticker\": \"7203\",\n      \"filer_name_ja\": \"トヨタ自動車株式会社\",\n      \"doc_type_code\": \"220\",\n      \"description_ja\": \"自己株券買付状況報告書（法２４条の６第１項に基づくもの）\",\n      \"subject_edinet_code\": null,\n      \"issuer_edinet_code\": null,\n      \"parent_doc_id\": null,\n      \"withdrawn\": 0,\n      \"has_xbrl\": 1,\n      \"has_csv\": 1,\n      \"has_english\": 0\n    }\n  ],\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  status: "{\n  \"ok\": true,\n  \"latest_large_holding_submit_date\": \"2026-10-29\"\n}",
  hooks: "{\n  \"data\": []\n}",
  unw: "{\n  \"data\": [\n    {\n      \"name_en\": \"DENSO CORPORATION\",\n      \"ticker\": \"6902\",\n      \"fiscal_year_end\": \"2026-03-31\",\n      \"source_doc_id\": \"S100Y9T1\",\n      \"named_holdings\": 12,\n      \"named_holdings_linked_to_ticker\": 5,\n      \"named_book_value_jpy\": 83994000000,\n      \"mutual_as_stated\": 1,\n      \"mutual_confirmed_both_reports\": 1,\n      \"named_reduced\": 2,\n      \"named_increased\": 1,\n      \"named_exited\": 4,\n      \"held_by_listed_companies\": 16,\n      \"listed_issues\": 8,\n      \"listed_carrying_amount_jpy\": 83994000000,\n      \"listed_issues_increased\": 1,\n      \"listed_acquisition_cost_jpy\": 33250000000,\n      \"listed_issues_decreased\": 6,\n      \"listed_sale_amount_jpy\": 450945000000\n    }\n  ],\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",

  tr: "{\n  \"by_holder\": [\n    {\n      \"filer_edinet_code\": \"E24872\",\n      \"holder\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー (Silchester International Investors LLP)\",\n      \"ticker\": \"4042\",\n      \"first_trade_date\": \"2026-09-02\",\n      \"last_trade_date\": \"2026-10-21\",\n      \"shares_bought\": 3082900,\n      \"shares_sold\": 0,\n      \"net_shares\": 3082900,\n      \"net_pct_of_outstanding\": 0.92,\n      \"disclosed_vwap_jpy\": 2610.92,\n      \"disclosed_low_jpy\": 2577,\n      \"disclosed_high_jpy\": 2706\n    }\n  ],\n  \"data\": [\n    {\n      \"trade_date\": \"2026-10-21\",\n      \"ticker\": \"4042\",\n      \"filer_edinet_code\": \"E24872\",\n      \"holder\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー (Silchester International Investors LLP)\",\n      \"security_ja\": \"普通株式\",\n      \"shares\": 191800,\n      \"pct_of_outstanding\": 0.06,\n      \"venue\": \"on_market\",\n      \"side\": \"acquisition\",\n      \"unit_price_jpy\": null,\n      \"kind\": \"trade\",\n      \"common\": true,\n      \"note_ja\": null,\n      \"source_doc_id\": \"S100Z9GY\",\n      \"filing_date\": \"2026-10-22\"\n    }\n  ],\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  invtr: "{\n  \"by_holder\": [\n    {\n      \"filer_edinet_code\": \"E24872\",\n      \"holder\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー (Silchester International Investors LLP)\",\n      \"ticker\": \"4536\",\n      \"first_trade_date\": \"2026-09-02\",\n      \"last_trade_date\": \"2026-10-23\",\n      \"shares_bought\": 676700,\n      \"shares_sold\": 0,\n      \"net_shares\": 676700,\n      \"net_pct_of_outstanding\": 0.2,\n      \"disclosed_vwap_jpy\": 1701.19,\n      \"disclosed_low_jpy\": 1652,\n      \"disclosed_high_jpy\": 1891\n    }\n  ],\n  \"data\": [\n    {\n      \"trade_date\": \"2026-10-23\",\n      \"ticker\": \"4536\",\n      \"filer_edinet_code\": \"E24872\",\n      \"holder\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー (Silchester International Investors LLP)\",\n      \"security_ja\": \"普通株式\",\n      \"shares\": 109300,\n      \"pct_of_outstanding\": 0.03,\n      \"venue\": \"on_market\",\n      \"side\": \"acquisition\",\n      \"unit_price_jpy\": null,\n      \"kind\": \"trade\",\n      \"common\": true,\n      \"note_ja\": null,\n      \"source_doc_id\": \"S100Z9VH\",\n      \"filing_date\": \"2026-10-26\"\n    }\n  ],\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  inv: "{\n  \"filer_edinet_code\": \"E24872\",\n  \"filer_name_ja\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー\",\n  \"filer_name_en\": \"Silchester International Investors LLP\",\n  \"joint_holder_names\": [\n    \"シルチェスター・インターナショナル・インベスターズ・エルエルピー (Silchester International Investors LLP)\"\n  ],\n  \"latest_positions\": [\n    {\n      \"ticker\": \"4536\",\n      \"issuer_name_en\": \"SANTEN PHARMACEUTICAL CO.,LTD.\",\n      \"issuer_name_ja\": \"参天製薬株式会社\",\n      \"holding_ratio_pct\": 10.25,\n      \"as_of\": \"2026-10-26\",\n      \"doc_id\": \"S100Z9VH\"\n    }\n  ],\n  \"filings\": [\n    {\n      \"doc_id\": \"S100Z9VH\",\n      \"report_kind\": \"change_report\",\n      \"report_title_ja\": \"変更報告書No. 6\",\n      \"filer_name_ja\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー\",\n      \"filer_edinet_code\": \"E24872\",\n      \"obligation_date\": \"2026-10-23\",\n      \"submit_date\": \"2026-10-26\",\n      \"holders_count\": 1,\n      \"change_reason_ja\": \"株券等保有割合が前回の報告書提出時より1%以上増加したこと\",\n      \"issuer_name_ja\": \"参天製薬株式会社\",\n      \"issuer_ticker\": \"4536\",\n      \"exchange_ja\": \"東京証券取引所(プライム市場)\",\n      \"holding_ratio_pct\": 10.25,\n      \"previous_ratio_pct\": 9.23,\n      \"ratio_change_pt\": 1.02,\n      \"purpose_ja\": \"提出者は、発行者に対し、企業価値及び株主価値の向上を目的として、資本政策の変更、資本効率の向上、事業構造の見直し、及びコーポレート・ガバナンスの強化等に関する提案（重要提案行為等に該当し得るものを含む。）を行う方針です。これらの提案に…\",\n      \"purpose_tags\": [\n        \"may_make_proposals\"\n      ],\n      \"source_url\": \"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100Z9VH.pdf\",\n      \"source\": \"EDINET (Financial Services Agency of Japan)\",\n      \"pdf_ratio_pct\": 10.25,\n      \"ratio_source\": \"xbrl\",\n      \"issuer_name_en\": \"SANTEN PHARMACEUTICAL CO.,LTD.\",\n      \"issuer_edinet_code\": \"E00949\",\n      \"issuer_corporate_number\": \"5120001051819\",\n      \"filer_name_en\": null,\n      \"holders\": [\n        {\n          \"shares\": 33050300,\n          \"ratio_pct\": 10.25,\n          \"previous_ratio_pct\": 9.23,\n          \"own_funds_jpy\": 0,\n          \"borrowings_jpy\": 0,\n          \"holder_type_ja\": \"法人(英国法に基づく有限責任事業組合(LLP))\",\n          \"name\": \"シルチェスター・インターナショナル・インベスターズ・エルエルピー (Silchester International Investors LLP)\",\n          \"purpose_ja\": \"提出者は、発行者に対し、企業価値及び株主価値の向上を目的として、資本政策の変更、資本効率の向上、事業構造の見直し、及びコーポレート・ガバナンスの強化等に関する提案(重要提案行為等に該当し得るものを含む。)を行う方針です。これらの提案に…\",\n          \"holder_type\": \"corporation\"\n        }\n      ],\n      \"filer_name_en_filing\": \"Silchester International Investors LLP\"\n    }\n  ],\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  pui: "{\n  \"data\": [\n    {\n      \"month\": \"2025-11\",\n      \"reports\": 150,\n      \"mean_policy_uncertainty\": 0.633,\n      \"share_of_reports_mentioning\": 0.607,\n      \"share_by_area\": {\n        \"trade\": 0.373,\n        \"fiscal_tax\": 0.06,\n        \"monetary\": 0.147,\n        \"regulation\": 0.107,\n        \"politics\": 0.44\n      }\n    }\n  ]\n}",
  fin: "{\n  \"ticker\": \"7203\",\n  \"accounting_standard\": \"IFRS\",\n  \"fiscal_year_end\": \"2026-03-31\",\n  \"key_metrics\": [\n    {\n      \"fiscal_year_end\": \"2026-03-31\",\n      \"basis\": \"consolidated\",\n      \"revenue\": 50684952000000,\n      \"ordinary_income\": null,\n      \"profit_before_tax\": 5152996000000,\n      \"net_income\": 3848098000000,\n      \"total_assets\": 105522331000000,\n      \"net_assets\": 39918854000000,\n      \"equity_ratio\": 37.8,\n      \"roe\": 10.1,\n      \"eps\": 295.25,\n      \"bps\": 3062.82,\n      \"dividend_per_share\": 95,\n      \"payout_ratio\": 36.4,\n      \"operating_cf\": 5472920000000,\n      \"investing_cf\": -1520307000000,\n      \"financing_cf\": -536659000000,\n      \"cash\": 12659622000000,\n      \"shares_issued\": 15794987000,\n      \"employees\": 390927\n    }\n  ],\n  \"statements\": {\n    \"income_statement\": {\n      \"current\": {\n        \"CostOfSalesIFRS\": 39141418000000,\n        \"SellingGeneralAndAdministrativeExpensesIFRS\": 4697524000000,\n        \"OperatingProfitLossIFRS\": 3766216000000\n      }\n    },\n    \"balance_sheet\": {\n      \"current\": {\n        \"TradeAndOtherReceivablesCAIFRS\": 3795986000000,\n        \"OtherFinancialAssetsCAIFRS\": 3982445000000,\n        \"InventoriesCAIFRS\": 5134996000000\n      }\n    },\n    \"cash_flow\": {\n      \"current\": {\n        \"CashAndCashEquivalentsIFRS\": 12659622000000,\n        \"DepreciationAndAmortizationOpeCFIFRS\": 2392519000000,\n        \"ShareOfLossProfitOfInvestmentsAccountedForUsingEquityMethodOpeCFIFRS\": -552742000000\n      }\n    }\n  },\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  det: "{\n  \"ticker\": \"7203\",\n  \"fiscal_year_end\": \"2026-03-31\",\n  \"english_name\": \"TOYOTA MOTOR CORPORATION\",\n  \"auditor\": \"PwC Japan有限責任監査法人\",\n  \"audit_fees_jpy\": 1107000000,\n  \"non_audit_fees_jpy\": 3000000,\n  \"capex_jpy\": 2284125000000,\n  \"total_shareholder_return_5y\": 2.051,\n  \"issued_shares\": 15794987460,\n  \"treasury_shares\": 2761055400,\n  \"female_officer_ratio_pct\": 20,\n  \"workforce\": {\n    \"employees\": 73133,\n    \"average_age\": 40.5,\n    \"average_tenure_years\": 15.1,\n    \"average_annual_salary_jpy\": 10060464,\n    \"female_manager_ratio_pct\": 3.9\n  },\n  \"major_shareholders\": [\n    {\n      \"shares\": 1667971000,\n      \"name\": \"日本マスタートラスト信託銀行㈱\",\n      \"ratio_pct\": 12.8\n    }\n  ],\n  \"officers\": [],\n  \"shareholder_categories_pct\": {\n    \"national_and_local_governments\": 0,\n    \"financial_institutions\": 27.01,\n    \"financial_service_providers\": 1.47,\n    \"other_corporations\": 20.29,\n    \"foreigners_other_than_individuals\": 22.79,\n    \"foreign_individuals\": 0.01,\n    \"individuals_and_others\": 28.43\n  },\n  \"source_doc_id\": \"S100Y8NY\",\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\"\n}",
  funds: "{\n  \"ticker\": \"7203\",\n  \"quarter\": \"2026q3\",\n  \"fund_count\": 375,\n  \"total_shares\": 1089974907,\n  \"total_value_usd\": 20098840148.44,\n  \"latest_report_date\": \"2026-07-31\",\n  \"top_funds\": [\n    {\n      \"fund\": \"VANGUARD TOTAL INTERNATIONAL STOCK INDEX FUND\",\n      \"registrant\": \"VANGUARD STAR FUNDS\",\n      \"series_id\": \"S000002932\",\n      \"shares\": 157195113,\n      \"value_usd\": 2974867603.22,\n      \"pct_of_fund\": 0.460603185459,\n      \"report_date\": \"2026-07-31\",\n      \"accession\": \"0000736054-26-000275\"\n    }\n  ],\n  \"source\": \"SEC Form N-PORT data sets (U.S. Securities and Exchange Commission)\",\n  \"attribution\": \"Source: SEC Form N-PORT data sets (U.S. Securities and Exchange Commission); mapped to Tokyo tickers via OpenFIGI. Not investment advice.\"\n}",
  xh: "{\n  \"ticker\": \"7203\",\n  \"summary\": {\n    \"ticker\": \"7203\",\n    \"fiscal_year_end\": \"2026-03-31\",\n    \"source_doc_id\": \"S100Y8NY\",\n    \"named_holdings\": 37,\n    \"named_holdings_linked_to_ticker\": 27,\n    \"named_book_value_jpy\": 3237499000000,\n    \"mutual_as_stated\": 20,\n    \"mutual_confirmed_both_reports\": 18,\n    \"named_reduced\": 1,\n    \"named_increased\": 4,\n    \"named_exited\": 3,\n    \"held_by_listed_companies\": 114,\n    \"listed_issues\": 34,\n    \"listed_carrying_amount_jpy\": 3237498000000,\n    \"listed_issues_increased\": 6,\n    \"listed_acquisition_cost_jpy\": 93737000000,\n    \"listed_issues_decreased\": 5,\n    \"listed_sale_amount_jpy\": 238784000000\n  },\n  \"holds\": [\n    {\n      \"ticker\": \"9433\",\n      \"name_en\": \"KDDI CORPORATION\",\n      \"holder\": \"reporting_company\",\n      \"kind\": \"specified\",\n      \"name_ja\": \"ＫＤＤＩ㈱\",\n      \"shares\": 363365900,\n      \"prior_shares\": 203294600,\n      \"shares_change_pct\": 78.7,\n      \"book_value_jpy\": 989627000000,\n      \"prior_book_value_jpy\": 959347000000,\n      \"held_back_as_stated\": true,\n      \"reciprocal_confirmed\": true,\n      \"purpose_ja\": \"自動車関連（情報）取引の維持・発展[株式数が変動した理由]株式分割、一部売却のため\"\n    }\n  ],\n  \"held_by\": [\n    {\n      \"holder_ticker\": \"8306\",\n      \"holder_name_en\": \"Mitsubishi UFJ Financial Group, Inc.\",\n      \"holder_name_ja\": \"株式会社三菱ＵＦＪフィナンシャル・グループ\",\n      \"fiscal_year_end\": \"2026-03-31\",\n      \"source_doc_id\": \"S100YJQO\",\n      \"holder\": \"largest_holding_company\",\n      \"kind\": \"specified\",\n      \"name_ja\": \"トヨタ自動車株式会社\",\n      \"shares\": 156830000,\n      \"prior_shares\": 156830000,\n      \"shares_change_pct\": 0,\n      \"book_value_jpy\": 495896000000,\n      \"prior_book_value_jpy\": 410267000000,\n      \"held_back_as_stated\": true,\n      \"reciprocal_confirmed\": true,\n      \"purpose_ja\": \"発行会社グループとの、主に金融取引(預金・貸出・為替等)における総合的な取引関係維持・拡大のために保有\"\n    }\n  ]\n}",
  ind: "{\n  \"ticker\": \"7203\",\n  \"name_en_filing\": \"TOYOTA MOTOR CORPORATION\",\n  \"fiscal_year_end\": \"2026-03-31\",\n  \"source_doc_id\": \"S100Y8NY\",\n  \"policy_uncertainty\": {\n    \"policy_uncertainty_per_100\": 0.47,\n    \"policy_uncertainty_mentions\": 2,\n    \"policy_uncertainty_by_area\": {\n      \"trade\": 2,\n      \"fiscal_tax\": 0,\n      \"monetary\": 0,\n      \"regulation\": 1,\n      \"politics\": 1\n    },\n    \"uncertainty_per_100\": 0.7,\n    \"percentile\": 31.6,\n    \"note\": \"Share of disclosure text discussing policy-related uncertainty, by policy area; proprietary text method. Descriptive, not a forecast.\"\n  },\n  \"rate_lens\": {\n    \"data_warning\": null,\n    \"shock_bp\": 50,\n    \"fiscal_year_end\": \"2026-03-31\",\n    \"interest_bearing_debt_jpy\": 43205469000000,\n    \"debt_lines\": {\n      \"InterestBearingLiabilitiesCLIFRS\": 17581104000000,\n      \"InterestBearingLiabilitiesNCLIFRS\": 25624365000000\n    },\n    \"cash_jpy\": 12659622000000,\n    \"extra_annual_interest_jpy\": 216027345000,\n    \"reported_interest_expense_jpy\": null,\n    \"extra_interest_pct_of_operating_cf\": 3.95,\n    \"implied_interest_rate_pct\": null,\n    \"note\": \"Arithmetic example: reported interest-bearing debt x 50 basis points for one year. It is not a forecast. Fixed-rate d…\"\n  },\n  \"attribution\": \"Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.\",\n  \"financial_percentiles\": {\n    \"metrics\": {\n      \"roe\": {\n        \"value\": 10.1,\n        \"percentile\": 59.0\n      },\n      \"ordinary_margin\": {\n        \"value\": 0.1017,\n        \"percentile\": 68.7\n      },\n      \"equity_ratio\": {\n        \"value\": 37.8,\n        \"percentile\": 23.8\n      },\n      \"revenue_growth\": {\n        \"value\": 0.0551,\n        \"percentile\": 52.9\n      },\n      \"net_income_growth\": {\n        \"value\": -0.1924,\n        \"percentile\": 23.0\n      },\n      \"ocf_to_assets\": {\n        \"value\": 0.0519,\n        \"percentile\": 41.7\n      }\n    },\n    \"mean_percentile\": 44.8,\n    \"mean_percentile_within_sector\": 39.7,\n    \"note\": \"Percentile rank among listed companies. mean_percentile averages the available metric percentiles; it is not a rating or evaluation.\"\n  },\n  \"results_wording_balance\": {\n    \"balance\": -0.1,\n    \"words\": {\n      \"positive\": 18,\n      \"negative\": 22\n    },\n    \"note\": \"Balance of positive and negative results wording; proprietary text method. It counts wording only, does not interpret context and says nothing about results or prospects.\"\n  },\n  \"ownership_activity\": {\n    \"filers_stating_may_make_proposals_365d\": 0,\n    \"new_5pct_holders_180d\": 0,\n    \"net_stake_change_pt_90d\": 0,\n    \"held_by_listed_companies_pct\": 7.82,\n    \"note\": \"…\"\n  }\n}",
  rel: "{\n  \"ticker\": \"7203\",\n  \"data\": [\n    {\n      \"consolidated\": true,\n      \"results\": {\n        \"profit_before_tax_mn_jpy\": 2478127,\n        \"profit_before_tax_yoy_pct\": -9.3,\n        \"net_income_parent_mn_jpy\": 1773426,\n        \"net_income_parent_yoy_pct\": -7.0\n      },\n      \"ticker\": \"7203\",\n      \"fiscal_year_end_month\": \"2026-03\",\n      \"period\": \"H1\",\n      \"release_date\": \"2025-11-13\",\n      \"source\": \"edinet_half_year\",\n      \"source_doc_id\": \"S100WYZE\",\n      \"source_url\": \"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100WYZE\",\n      \"summary_en\": \"For the first half of the fiscal year to 2026-03: Profit before tax ¥2.48 trillion (-9.3% YoY), Net income ¥1.77 trillion (-7.0% YoY).\"\n    }\n  ]\n}",
  rels: "{\n  \"data\": [\n    {\n      \"name_en\": \"TOYOTA MOTOR CORPORATION\",\n      \"ticker\": \"7203\",\n      \"period\": \"H1\",\n      \"release_date\": \"2025-11-13\",\n      \"source\": \"edinet_half_year\",\n      \"source_doc_id\": \"S100WYZE\",\n      \"summary_en\": \"For the first half of the fiscal year to 2026-03: Profit before tax ¥2.48 trillion (-9.3% YoY), Net income ¥1.77 trillion (-7.0% YoY).\"\n    }\n  ]\n}",
  brief: "{\n  \"ticker\": \"7203\",\n  \"name_en_filing\": \"TOYOTA MOTOR CORPORATION\",\n  \"fiscal_year_end\": \"2026-03-31\",\n  \"latest_results\": {\n    \"period\": \"H1\",\n    \"release_date\": \"2025-11-13\",\n    \"source\": \"edinet_half_year\",\n    \"source_doc_id\": \"S100WYZE\",\n    \"summary_en\": \"For the first half of the fiscal year to 2026-03: Profit before tax ¥2.48 trillion (-9.3% YoY), Net income ¥1.77 trillion (-7.0% YoY).\"\n  },\n  \"indicators\": {\n    \"financial_percentiles\": {\n      \"mean_percentile\": 44.8,\n      \"mean_percentile_within_sector\": 39.7,\n      \"metrics\": \"…\"\n    }\n  },\n  \"ownership_events_180d\": [],\n  \"rate_lens\": {\n    \"shock_bp\": 50,\n    \"interest_bearing_debt_jpy\": 43205469000000,\n    \"extra_annual_interest_jpy\": 216027345000,\n    \"extra_interest_pct_of_operating_cf\": 3.95\n  }\n}",

  hookcreate: "{\n  \"id\": \"wh_3233d32e87ea2021\",\n  \"url\": \"https://example.com/hook\",\n  \"types\": [\n    \"stated_may_make_proposals\"\n  ],\n  \"tickers\": [\n    \"7203\"\n  ],\n  \"secret\": \"whsec_…shown once…\",\n  \"created_at\": \"2026-10-31T08:36:04.246Z\",\n  \"delivering_events_first_seen_after\": \"2026-10-31T08:36:04.246Z\"\n}",
  hooklist: "{\n  \"data\": [\n    {\n      \"id\": \"wh_3233d32e87ea2021\",\n      \"url\": \"https://example.com/hook\",\n      \"types\": [\n        \"stated_may_make_proposals\"\n      ],\n      \"tickers\": [\n        \"7203\"\n      ],\n      \"created_at\": \"2026-10-31T08:36:04.246Z\",\n      \"enabled\": true,\n      \"delivered_events\": 0,\n      \"consecutive_failures\": 0,\n      \"last_attempt_at\": null,\n      \"last_status\": null,\n      \"last_error\": null\n    }\n  ]\n}",
};

// ---------- Marketing, docs, legal ----------

export function pricing(user: User | null, earliest: string | null = null, open = false) {
  const y = `<span class="chip accent">Yes</span>`, n = `<span class="faint">—</span>`;
  const matrix: [string, string, string][] = [
    ["Requests a month", fmtNum(QUOTA.starter), fmtNum(QUOTA.pro)],
    ["Requests a minute", fmtNum(RPM.starter), fmtNum(RPM.pro)],
    ["Every endpoint and MCP tool, full history, webhooks", y, y],
    ["5% filings, ownership changes, disclosed trades, investor look-up", y, y],
    ["Tender offers, shareholder votes, cross-shareholdings, governance", y, y],
    ["Financial statements, segments, half-year results, stated credit ratings", y, y],
    ["U.S. funds per stock (SEC Form N-PORT), company briefs", y, y],
    ["Filing-based screener, sorted lists, CSV output", y, y],
  ];
  const faq: [string, string][] = [
    ["Can I subscribe now?", open ? "Yes. Sign in and subscribe here or on your dashboard. Paddle, our merchant of record, takes the payment and handles VAT or sales tax. If you cancel, your plan stays until the end of the paid period." : "Not yet. Billing is not connected, so no plan can be bought and nothing is charged. The Starter and Pro prices are planned and may change before they go on sale."],
    ["Where does the data come from?", "EDINET, the Financial Services Agency's disclosure system, read through its API, plus SEC Form N-PORT and OpenFIGI. See Sources and licences."],
    ["How much history is there?", `5% filings on file start ${earliest ?? "recently"}. Annual reports start June 2026, and each carries five years of headline figures.`],
    ["Do you provide stock prices?", "No. Each company record includes a FIGI and, where our mapping covers it, common ticker symbols (7203.T, 7203 JP), so you can join your own price source. The only unit prices served are those a filer states in the trade table of a 5% report. They are filed prices, not market prices."],
    ["How fresh is it?", "The ingest job is scheduled every 10 minutes from 08:00 to 21:00 Japan time on business days, and webhook delivery runs every 5 minutes. The service has not launched yet, so these are the schedules rather than a track record."],
    ["What happens at the request limit?", "HTTP 429 means one of two limits was hit: the monthly quota (it resets at the start of the next calendar month, UTC) or the per-minute limit (retry after the delay in the message). The message says which. Successful responses report usage in X-Quota-Used and X-Quota-Limit."],
    ["Is this investment advice?", "No. It is reference data taken from public filings, with descriptive figures that we calculate and label as ours. Percentiles are ranks among listed companies, and lists are sorted only by the metric you choose. Data may be incomplete, delayed or wrong, so check the original filing. Nothing here is a recommendation to buy or sell any security."],
  ];
  return page("Pricing | japanfilings", `<div class="wrap">
<div class="co-head"><h1>Pricing</h1><p class="lead">Two plans, both with every dataset, full history and webhooks. Starter is $9 a month; Pro is $39 for higher request limits${open ? "" : ". Plans go on sale at launch"}.</p></div>
${open ? OPEN_NOTE : BILLING_NOTE}
<div style="margin-top:16px">${pricingCards(open, user)}</div>
<p class="faint xs" style="margin-top:14px">Prices in U.S. dollars, excluding any VAT or sales tax. Not launched yet. Reference data, not investment advice; no share prices.</p>
<section style="margin-top:32px"><h2 style="margin-bottom:16px">What each plan includes</h2>
<div class="panel scroll"><table><tr><th></th><th>Starter</th><th>Pro</th></tr>
${matrix.map((r) => `<tr><td>${esc(r[0])}</td><td class="small">${r[1]}</td><td class="small">${r[2]}</td></tr>`).join("")}</table></div></section>
<section><h2 style="margin-bottom:16px">Questions</h2><div class="stack" style="gap:18px;max-width:76ch">
${faq.map(([q, a]) => `<div><h3>${esc(q)}</h3><p class="muted" style="margin-top:4px">${esc(a)}</p></div>`).join("")}</div></section>
</div>`, user, { path: "/pricing", description: open ? "japanfilings plans: Starter at $9 and Pro at $39 a month, both with every dataset." : "japanfilings plans: Starter at $9 and Pro at $39 a month, both with every dataset, on sale at launch." });
}

// ---------- Documentation ----------

type Ep = { m: string; p: string; plan?: "pro" | "business"; d: string; q?: [string, string][]; call?: string; ex?: string; body?: string; note?: string };

const SINCE: [string, string] = ["since", "Earliest date, YYYY-MM-DD"];
const lim = (def: number, max: number): [string, string] => ["limit", `Rows to return. Default ${def}, maximum ${max}`];

const DOC_ENDPOINTS: [string, string, string, Ep[]][] = [
  ["companies", "Companies and financials", "Identifiers, statements and governance from the latest annual securities report.", [
    { m: "GET", p: "/v1/companies/search", d: "Find listed companies by ticker or by English or Japanese name.", q: [["q", "Required. Ticker or name"], lim(50, 50)], call: "/v1/companies/search?q=toyota&limit=1", ex: "search" },
    { m: "GET", p: "/v1/companies/{ticker}", d: "Company profile: names, corporate number, industry, FIGI and common ticker symbols.", call: "/v1/companies/7203", ex: "company" },
    { m: "GET", p: "/v1/companies/{ticker}/financials", d: "Five years of key figures, plus current and prior income statement, balance sheet and cash flow. Statement items use XBRL element names.", call: "/v1/companies/7203/financials", ex: "fin" },
    { m: "GET", p: "/v1/companies/{ticker}/details", d: "Officer titles (no names), workforce, major shareholders, shareholder mix, auditor and fees. Shareholders not recognisably organisations appear as individual holder.", call: "/v1/companies/7203/details", ex: "det" },
    { m: "GET", p: "/v1/companies/{ticker}/segments", d: "Segments stated in the latest annual securities report.", call: "/v1/companies/7203/segments" },
    { m: "GET", p: "/v1/companies/{ticker}/voting", d: "Shareholder-meeting voting results for one company.", q: [SINCE, lim(50, 200)], call: "/v1/companies/7203/voting" },
    { m: "GET", p: "/v1/companies/{ticker}/tender-offers", d: "Tender-offer notifications, results and target opinions for one company.", q: [SINCE, lim(50, 200)], call: "/v1/companies/5484/tender-offers" },
    { m: "GET", p: "/v1/voting", d: "Voting results across companies.", q: [SINCE, lim(50, 500)], call: "/v1/voting?limit=1" },
    { m: "GET", p: "/v1/tender-offers", d: "Tender-offer filings across companies.", q: [SINCE, lim(50, 500)], call: "/v1/tender-offers?limit=1" },
    { m: "GET", p: "/v1/filings", d: "Metadata of EDINET filings. doc_type_code is EDINET's own code.", q: [["ticker", "Filter by ticker"], ["doc_type", "EDINET document type code"], SINCE, lim(50, 200)], call: "/v1/filings?ticker=7203&limit=1", ex: "filings" },
  ]],
  ["events", "Ownership changes", "Events derived from 5% rule filings, the filings themselves and the filers behind them. All plans: full history, and the response names the cut-off in history_limited_to.", [
    { m: "GET", p: "/v1/events", d: "Ownership-change events with one-line English summaries.", q: [["ticker", "Filter by the company"], ["filer", "Filter by filer EDINET code"], ["type", "Comma-separated: new_stake, stake_increase, stake_decrease, exit_below_5pct, threshold_crossed, stated_may_make_proposals, purpose_changed, amendment"], SINCE, lim(50, 200)], call: "/v1/events?type=stated_may_make_proposals&limit=1", ex: "ev" },
    { m: "GET", p: "/v1/large-holdings", d: "The 5% filings: holders, purposes, change against the previous report.", q: [["ticker", "Filter by the company"], ["filer", "Filer EDINET code"], SINCE, lim(50, 200)], call: "/v1/large-holdings?ticker=4042&limit=1", ex: "lh" },
    { m: "GET", p: "/v1/investors/{code}", d: "One filer's filings, its latest stake in each company, and joint holders.", q: [lim(50, 500)], call: "/v1/investors/E24872?limit=1", ex: "inv" },
    { m: "GET", p: "/v1/companies/{ticker}/trades", d: "Rows of the 60-day trade tables in 5% reports on this company, with net shares and the average disclosed price per holder. Unit prices are usually stated only for off-market trades; they are not market prices.", q: [SINCE, lim(50, 1000)], call: "/v1/companies/4042/trades?limit=1", ex: "tr" },
    { m: "GET", p: "/v1/investors/{code}/trades", d: "The same trade rows for one filer across all companies.", q: [SINCE, lim(50, 1000)], call: "/v1/investors/E24872/trades?limit=1", ex: "invtr" },
  ]],
  ["results", "Half-year and quarterly results", "Results between annual reports, from half-year and quarterly securities reports on EDINET (source: edinet_half_year), each with its source filing. They arrive some weeks after the period ends and state no forecast.", [
    { m: "GET", p: "/v1/companies/{ticker}/earnings-releases", d: "Period results with year-on-year change as filed. Amounts in millions of yen.", q: [lim(40, 40)], call: "/v1/companies/7203/earnings-releases?limit=1", ex: "rel" },
    { m: "GET", p: "/v1/earnings-releases", d: "Latest period results across companies, newest filing first.", q: [SINCE, lim(50, 500)], call: "/v1/earnings-releases?limit=1", ex: "rels" },
  ]],
  ["cross", "Cross-shareholdings", "From the status of shareholdings section of annual reports. Names are matched to tickers by Japanese name, so renamed or delisted companies may stay unmatched. Share changes include stock splits.", [
    { m: "GET", p: "/v1/companies/{ticker}/cross-holdings", d: "Strategic stakes the company holds, listed companies holding it, mutual pairs and the year's sales and purchases.", call: "/v1/companies/7203/cross-holdings", ex: "xh" },
    { m: "GET", p: "/v1/cross-holdings/unwinding", d: "Largest stated sales of listed strategic shares in the latest fiscal year.", q: [lim(50, 200)], call: "/v1/cross-holdings/unwinding?limit=1", ex: "unw" },
  ]],
  ["indicators", "Indicators", "Descriptive figures computed from filings. Percentiles are ranks among listed companies. Text indicators use a proprietary method. None of them is a rating.", [
    { m: "GET", p: "/v1/companies/{ticker}/indicators", d: "Financial percentiles, policy-uncertainty counts, results wording balance, ownership activity counts and an interest example on reported debt.", call: "/v1/companies/7203/indicators", ex: "ind" },
    { m: "GET", p: "/v1/indicators/sorted", d: "Companies in the order of one figure or percentile. The order is arithmetic and is not a judgement.", q: [["metric", "Required. roe, equity_ratio, mean_percentile, policy_uncertainty, new_5pct_holders_180d and others; an error lists them all"], ["order", "desc (default) or asc"], ["sector", "Sector slugs, comma-separated"], lim(50, 1000)], call: "/v1/indicators/sorted?metric=roe&sector=banks&limit=1" },
    { m: "GET", p: "/v1/indicators/policy-uncertainty", d: "Japan corporate policy-uncertainty index by filing month, with the share of reports per policy area.", call: "/v1/indicators/policy-uncertainty", ex: "pui" },
  ]],
  ["brief", "Company brief", "One company from its own filings in one response, each part with its date.", [
    { m: "GET", p: "/v1/companies/{ticker}/brief", d: "A company's latest period results, indicator figures, ownership changes in the last 180 days, latest buyback report, governance filings, stated ratings and a 50 bp interest example on its reported debt. rate_lens is null where no comparable debt is reported, as for banks.", call: "/v1/companies/7203/brief", ex: "brief" },
  ]],
  ["ratings", "Credit ratings stated in filings", "Ratings as issuers state them in bond filings on EDINET, each with its source filing. A row says what the filing said on its date; agencies can change or withdraw a rating. We do not rate anything, and symbols are not comparable across agencies.", [
    { m: "GET", p: "/v1/companies/{ticker}/credit-ratings", d: "Agency, rating, date, bond and source filing for one company. Issuers without a recent rated bond return an empty list.", call: "/v1/companies/3382/credit-ratings" },
    { m: "GET", p: "/v1/companies/{ticker}/buybacks", d: "Monthly share-buyback reports: limits per board resolution, shares and yen acquired, and progress as arithmetic on filed numbers.", q: [lim(50, 60)], call: "/v1/companies/7287/buybacks" },
    { m: "GET", p: "/v1/companies/{ticker}/governance", d: "Internal-control conclusions and auditor changes as the company states them, with short Japanese quotes and the source filing.", q: [lim(40, 40)], call: "/v1/companies/7203/governance" },
    { m: "GET", p: "/v1/governance", d: "The same filings across companies.", q: [["type", "internal_control or auditor_change"], SINCE, lim(50, 200)], call: "/v1/governance?type=auditor_change&limit=1" },
    { m: "GET", p: "/v1/cross-holdings/trades", d: "Trades and prices from 5% reports where the listed filer also names the stock among its strategic holdings. Why a holder traded is not stated.", q: [["ticker", "The stock traded"], ["holder", "The filer's ticker"], SINCE, lim(50, 200)], call: "/v1/cross-holdings/trades?limit=1" },
    { m: "GET", p: "/v1/credit-ratings", d: "Ratings stated in bond filings across issuers, including unlisted ones.", q: [["agency", "Filter by agency"], SINCE, lim(50, 200)], call: "/v1/credit-ratings?limit=1" },
  ]],
  ["funds", "U.S. funds", "From SEC Form N-PORT data sets: each fund's latest report in the quarter, in U.S. dollars as filed.", [
    { m: "GET", p: "/v1/companies/{ticker}/us-funds", d: "Fund count, total shares and value, and the top 25 funds.", call: "/v1/companies/7203/us-funds", ex: "funds" },
  ]],
  ["other", "Status", "", [
    { m: "GET", p: "/v1/status", d: "Data freshness. Needs no key.", call: "/v1/status", ex: "status" },
    { m: "GET", p: "/v1/freshness", d: "Freshness, source age, last run and row count for every dataset. Needs no key.", call: "/v1/freshness" },
    { m: "GET", p: "/v1/filings/latest", d: "Filing metadata newest first by when it was seen on EDINET's list.", q: [["ticker", "Filter by ticker"], SINCE, lim(50, 200)], call: "/v1/filings/latest?limit=1" },
    { m: "GET", p: "/v1/companies/{ticker}/refresh", d: "Latest refresh request and check status.", call: "/v1/companies/7203/refresh" },
    { m: "POST", p: "/v1/companies/{ticker}/refresh", d: "Queue a company re-check; joins an existing request when one is open." },
  ]],
];

const MCP_TOOLS: [string, string, string][] = [
  ["search_companies", "All plans", "Find companies by ticker or name"],
  ["get_company", "All plans", "Profile and identifiers"],
  ["get_financials", "All plans", "Key figures and statements"],
  ["get_company_details", "All plans", "Officer titles, workforce, shareholders, auditor"],
  ["get_company_segments", "All plans", "Segments from the latest annual report"],
  ["get_voting_results", "All plans", "Shareholder-meeting voting results"],
  ["get_tender_offers", "All plans", "Tender-offer filings and results"],
  ["get_ownership_events", "All plans", "Ownership-change events"],
  ["get_large_holdings", "All plans", "5% filings"],
  ["get_earnings_releases", "All plans", "Half-year and quarterly results from EDINET"],
  ["get_indicators", "All plans", "Indicators for one company"],
  ["get_policy_uncertainty_index", "All plans", "Monthly index"],
  ["list_companies", "All plans", "All listed companies, paged"],
  ["list_sectors", "All plans", "Sectors with company counts"],
  ["get_companies_batch", "All plans", "Up to 50 company profiles; counts per ticker"],
  ["get_financials_batch", "All plans", "Up to 50 companies' figures; counts per ticker"],
  ["get_company_peers", "All plans", "Same sector, nearest revenue"],
  ["get_sector_benchmarks", "All plans", "Sector medians and quartiles"],
  ["get_my_usage", "All plans", "Your plan, usage and limits"],
  ["get_disclosed_credit_ratings", "All plans", "Ratings as stated in bond filings"],
  ["get_share_buybacks", "All plans", "Monthly share-buyback reports"],
  ["get_governance_filings", "All plans", "Internal-control conclusions and auditor changes"],
  ["get_cross_holding_trades", "All plans", "Trades by listed strategic holders"],
  ["get_investor", "All plans", "A filer's stakes and joint holders"],
  ["search_investor_groups", "All plans", "Find investor groups by name"],
  ["get_investor_group_portfolio", "All plans", "A group's holdings, entries and exits"],
  ["get_investor_group_changes", "All plans", "A group's ownership events, paged"],
  ["get_investor_group_overlap", "All plans", "Groups with stakes in the same companies"],
  ["get_company_ownership_timeline", "All plans", "A company's ownership events by date"],
  ["get_cross_holding_unwinding_trades", "All plans", "Strategic-holding cuts beside disclosed trades"],
  ["get_disclosed_trades", "All plans", "Trades listed in 5% reports"],
  ["get_cross_holdings", "All plans", "A company's cross-shareholdings"],
  ["get_cross_holding_unwinding", "All plans", "Largest sales of strategic shares"],
  ["sort_companies_by_metric", "All plans", "Companies in the order of one metric"],
  ["get_company_brief", "All plans", "One company from its filings, on one page"],
  ["screen_companies", "All plans", "Filter companies on filing figures"],
  ["get_us_fund_holdings", "All plans", "U.S. funds holding a stock"],
];

export function docs(user: User | null, openapi: any, origin: string) {
  const o = esc(origin);
  const documented = new Set(DOC_ENDPOINTS.flatMap(([, , , eps]) => eps.map((e) => e.p)).concat(["/v1/webhooks", "/v1/webhooks/{id}", "/v1/webhooks/{id}/test"]));
  const undocumented = Object.keys(openapi.paths as Record<string, any>).filter((p) => !documented.has(p));
  const curl = (path: string) => `curl -H "Authorization: Bearer $JAPANFILINGS_KEY" \\\n  "${o}${esc(path)}"`;
  const example = (key?: string) => (key && DOC_EXAMPLES[key] ? `<details><summary>Example response</summary><pre><code>${esc(DOC_EXAMPLES[key])}</code></pre></details>` : "");
  const endpoint = (e: Ep) => `<div class="endpoint" id="ep-${esc(e.p.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, ""))}${e.m === "GET" ? "" : `-${e.m.toLowerCase()}`}"><div class="path"><b>${e.m}</b>${esc(e.p)}</div>${e.plan ? `<span class="plan ${e.plan}">${e.plan}</span>` : "<span></span>"}
<p>${esc(e.d)}</p>
${e.q?.length ? `<table class="params">${e.q.map(([n, v]) => `<tr><td><code>${esc(n)}</code></td><td>${esc(v)}</td></tr>`).join("")}</table>` : ""}
${e.call ? `<pre class="call"><code>${curl(e.call)}</code></pre>` : ""}${e.body ? `<pre class="call"><code>${e.body}</code></pre>` : ""}
${example(e.ex)}</div>`;
  const groups = DOC_ENDPOINTS.map(([id, title, intro, eps]) => `<h2 id="${id}">${title}</h2>${intro ? `<p>${esc(intro)}</p>` : ""}<div class="panel">${eps.map(endpoint).join("")}</div>`).join("");
  const postHook = `curl -X POST "${o}/v1/webhooks" \\\n  -H "Authorization: Bearer $JAPANFILINGS_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"url": "https://example.com/hook", "types": ["stated_may_make_proposals"], "tickers": ["7203"]}'`;
  const toc: [string, string][] = [["start", "Quick start"], ["auth", "Authentication"], ["conventions", "Conventions"], ["errors", "Errors"], ["limits", "Rate limits"], ["mcp", "MCP for agents"],
    ...DOC_ENDPOINTS.map(([id, t]): [string, string] => [id, t]), ["webhooks", "Webhooks"], ["verify", "Verifying signatures"], ["prices", "Prices"], ["gaps", "Known limits"]];
  return page("Documentation | japanfilings", `<div class="wrap docs">
<nav aria-label="Documentation">${toc.map(([id, t]) => `<a href="#${id}">${t}</a>`).join("")}</nav>
<div class="prose">
<span class="eyebrow">Documentation</span><h1>API and MCP</h1>
<p class="lead">JSON over HTTPS. Names are in English where the company registered one, with the Japanese original beside it in <code>*_ja</code> fields. Examples below use this site's own address (${o}) and are based on real responses captured on 2026-10-31 and reshaped to the current field names on 2026-11-01, shortened: arrays are cut to one item and long strings are clipped.</p>
<p class="small">Machine-readable spec: <a href="/openapi.json">/openapi.json</a>.</p>

<h2 id="start">Quick start</h2>
<ol><li><a href="/login">Sign in with your email</a>. We send a one-time link.</li><li>Choose a plan (Starter $9 or Pro $39 a month; both include everything). A key without a plan gets HTTP 402.</li><li>On the dashboard, choose <b>Create key</b> and copy it. It is shown once.</li><li>Call the API.</li></ol>
<pre><code>export JAPANFILINGS_KEY=jf_your_key

curl -H "Authorization: Bearer $JAPANFILINGS_KEY" \\
  "${o}/v1/companies/7203"</code></pre>
<details open><summary>Response</summary><pre><code>${esc(DOC_EXAMPLES.company)}</code></pre></details>
<p>Every plan is paid and includes every endpoint below; plans differ only in request limits. Billing is off until launch, so no plan can be bought yet; signup works only on a local test deployment for now.</p>
<p class="notice small">japanfilings provides reference data from public filings. It distributes no share prices and gives no investment advice, and nothing in the API or this documentation is a recommendation to buy or sell any security. Values we derive (events, net shares, percentiles, matches, interest examples) are labelled as ours, and prices in trade tables are filed prices, not market prices.</p>

<h2 id="auth">Authentication</h2>
<p>Send <code>Authorization: Bearer &lt;key&gt;</code> or <code>X-API-Key: &lt;key&gt;</code>. Keys start with <code>jf_</code>. An account has one active key, and replacing it on the dashboard revokes the old one at once. We keep only a hash, so a lost key has to be replaced. CORS is open on <code>/v1</code>, but keep keys out of public web pages.</p>

<h2 id="conventions">Conventions</h2>
<ul><li><b>Tickers</b> are Tokyo codes of four characters, such as <code>7203</code> or <code>648A</code>. Find them with <code>/v1/companies/search</code>.</li>
<li><b>Filers</b> are identified by EDINET code, such as <code>E24872</code>; find them in <code>filer_edinet_code</code>.</li>
<li><b>Amounts</b> are in yen unless the field name says otherwise: <code>_mn_jpy</code> means millions of yen and <code>_usd</code> means U.S. dollars. <b>Ratios</b> ending <code>_pct</code> are percent.</li>
<li><b>Dates</b> are <code>YYYY-MM-DD</code>.</li>
<li><b>Sources:</b> filing records carry <code>doc_id</code>, <code>source_doc_id</code> or <code>source_url</code>, and responses carry <code>attribution</code>. Keep the attribution when you show data to others.</li>
<li><b>Quotes</b> are excerpts of at most 200 characters, cut with an ellipsis, with personal names removed where we recognise them.</li>
<li><b>Language:</b> summaries and field names are in English. Purposes, shareholder names and quoted sentences stay in Japanese as filed.</li></ul>

<h2 id="errors">Errors</h2>
<p>Errors are JSON with one field: <code>{"error": "message"}</code>.</p>
<div class="panel scroll"><table><tr><th>Status</th><th>Meaning</th><th>Example message</th></tr>
<tr><td class="mono">400</td><td>A parameter or body is invalid.</td><td class="small"><code>Query parameter 'q' is required.</code></td></tr>
<tr><td class="mono">401</td><td>The key is missing, wrong or revoked.</td><td class="small"><code>Missing API key. Send 'Authorization: Bearer &lt;key&gt;'.</code></td></tr>
<tr><td class="mono">402</td><td>The account has no paid plan.</td><td class="small"><code>A paid plan is required. Plans start at $9 a month; see /pricing on our website.</code></td></tr>
<tr><td class="mono">404</td><td>No record for that ticker or ID, or no such endpoint.</td><td class="small"><code>No listed company for that ticker.</code></td></tr>
<tr><td class="mono">429</td><td>A limit was hit: either the monthly quota or the per-minute limit. The message says which.</td><td class="small"><code>Monthly quota of 20000 requests exceeded.</code> or <code>Rate limit of 60 requests per minute exceeded. Retry after 30 seconds.</code></td></tr>
<tr><td class="mono">500</td><td>Something failed on our side. Retry with a delay.</td><td class="small"><code>Internal error. Please retry; if it persists, contact support.</code></td></tr></table></div>

<h2 id="limits">Rate limits</h2>
<div class="panel scroll"><table><tr><th>Plan</th><th class="num">Requests a month</th><th>5% filings history</th></tr>
<tr><td>Starter (planned)</td><td class="num">${fmtNum(QUOTA.starter)}</td><td>All on file</td></tr><tr><td>Pro (planned)</td><td class="num">${fmtNum(QUOTA.pro)}</td><td>All on file</td></tr></table></div>
<p>The limit is per calendar month and resets on the 1st at 00:00 UTC. Per-minute limits are ${fmtNum(RPM.starter)} on Starter and ${fmtNum(RPM.pro)} on Pro, reported in the X-RateLimit-*-Minute headers; requests can briefly reach twice that across a minute boundary. Data responses carry X-Data-As-Of, and list endpoints accept format=csv. Every authenticated request counts, including ones that end in a 4xx error, and each MCP message counts as one, including <code>initialize</code> and <code>tools/list</code>. Successful responses carry <code>X-Quota-Used</code> and <code>X-Quota-Limit</code>.</p>

<h2 id="mcp">MCP for AI agents</h2>
<p>Remote MCP server over Streamable HTTP at <code>${o}/mcp</code>, using the same key and quota as the API. Authentication is by header. OAuth 2.1 sign-in for Claude and ChatGPT connectors is built and switched on per deployment, but has not yet been tested with those connectors.</p>
<h3>Claude Code</h3>
<pre><code>claude mcp add --transport http japanfilings ${o}/mcp \\
  --header "Authorization: Bearer $JAPANFILINGS_KEY"</code></pre>
<h3>Claude Desktop</h3>
<p>Add this to <code>claude_desktop_config.json</code> and restart the app. It runs the <code>mcp-remote</code> bridge, which needs Node.js.</p>
<pre><code>{
  "mcpServers": {
    "japanfilings": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "${o}/mcp", "--header", "Authorization: Bearer jf_your_key"]
    }
  }
}</code></pre>
<h3>Cursor</h3>
<p>Add this to <code>~/.cursor/mcp.json</code>, or <code>.cursor/mcp.json</code> in a project. Do not commit a file containing your key.</p>
<pre><code>{
  "mcpServers": {
    "japanfilings": {
      "url": "${o}/mcp",
      "headers": { "Authorization": "Bearer jf_your_key" }
    }
  }
}</code></pre>
<h3>Check the connection</h3>
<pre><code>curl -X POST "${o}/mcp" \\
  -H "Authorization: Bearer $JAPANFILINGS_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'</code></pre>
<h3>Tools</h3>
<p>Every tool is on every plan; plans differ only in request limits.</p>
<div class="panel scroll"><table><tr><th>Tool</th><th>Plan</th><th>Returns</th></tr>${MCP_TOOLS.map(([n, pl, d]) => `<tr><td><code>${n}</code></td><td class="small">${pl}</td><td class="small">${d}</td></tr>`).join("")}</table></div>

${groups}

<h2 id="webhooks">Webhooks</h2>
<p>Ownership changes pushed to your HTTPS URL. A delivery job runs every 5 minutes and sends events first seen after your webhook was created, filtered by type and ticker. Up to 5 webhooks per account, on every plan.</p>
<div class="panel">
${endpoint({ m: "POST", p: "/v1/webhooks", d: "Subscribe a URL. Optional: types, tickers (up to 500), and replay_from, an ISO time to receive events first seen after it. The secret is returned once.", body: postHook, ex: "hookcreate" })}
${endpoint({ m: "GET", p: "/v1/webhooks", d: "Your webhooks and their delivery status.", call: "/v1/webhooks", ex: "hooklist" })}
${endpoint({ m: "POST", p: "/v1/webhooks/{id}/test", d: "Send the latest stored event to the webhook, marked test: true. Returns the status your URL answered with." })}
${endpoint({ m: "DELETE", p: "/v1/webhooks/{id}", d: "Delete a webhook. Returns 204." })}
</div>
<p>Deliveries are POSTs with the body <code>{"webhook_id": "wh_…", "events": [ … ]}</code>, where each event has the shape of <code>/v1/events</code>. The host is resolved through DNS over HTTPS when registered and before every delivery; loopback, private, link-local, CGNAT and metadata addresses are refused. Redirects are not followed and count as failures. Answer with any 2xx within 10 seconds. A failed delivery is retried on each run, and the webhook pauses after 50 failures in a row, about four hours. Your dashboard shows the status.</p>
<h2 id="verify">Verifying signatures</h2>
<p>Each delivery has <code>X-JapanFilings-Signature: t=&lt;unix seconds&gt;,v1=&lt;hex&gt;</code>, an HMAC-SHA256 of <code>t + "." + raw body</code> with your secret.</p>
<pre><code>// Node.js
const [t, v1] = req.headers["x-japanfilings-signature"].split(",").map((p) =&gt; p.split("=")[1]);
const expected = crypto.createHmac("sha256", secret).update(t + "." + rawBody).digest("hex");
const valid = crypto.timingSafeEqual(Buffer.from(v1), Buffer.from(expected));

# Python
t, v1 = (p.split("=")[1] for p in sig.split(","))
expected = hmac.new(secret.encode(), f"{t}.{raw_body}".encode(), "sha256").hexdigest()
valid = hmac.compare_digest(v1, expected)</code></pre>

<h2 id="prices">Prices</h2>
<p>We do not distribute market prices. Use <code>figi</code>, <code>yahoo_symbol</code> or <code>bloomberg_ticker</code> from <code>/v1/companies/{ticker}</code> to join your own source. The only unit prices served are those a filer states in the trade table of a 5% report.</p>

<h2 id="gaps">Known limits</h2>
<ul><li>5% filings on file start in November 2021, and annual reports in June 2026. Each annual report carries five years of headline figures.</li>
<li>A newly listed company may have a page with its 5% filings before its profile and financials exist.</li>
<li>A company appears under financials once its annual report in that window has been processed.</li>
<li>No TDnet feed and no earnings releases. Results between annual reports come from half-year and quarterly securities reports on EDINET.</li>
<li>Individuals are not named in ownership data: 5% filers and holders who are not recognisably organisations appear as <code>individual filer</code> or <code>individual holder</code>.</li>
<li>Text indicators describe policy-uncertainty discussion and results wording; they do not interpret context or rate a company.</li>
<li>Three filers tag thousands of yen as yen in their cross-shareholding tables; their summaries carry <code>data_warning</code> and are left out of the largest-sales list.</li>
<li>Filings and our processing can both contain errors. Check important figures against the source filing.</li></ul>
${undocumented.length ? `<p class="faint xs">Also in the OpenAPI spec: ${undocumented.map((p) => `<code>${esc(p)}</code>`).join(" ")}</p>` : ""}
</div></div>`, user, { path: "/docs", description: "japanfilings API and MCP documentation: quick start, authentication, every endpoint with a real example, Claude and Cursor setup, webhooks, errors and rate limits." });
}

function legal(user: User | null, title: string, path: string, html: string, description: string) {
  return page(`${title} | japanfilings`, `<div class="wrap"><div class="prose" style="padding-block:40px">
<span class="eyebrow">Last updated 2026-11-01</span><h1>${title}</h1>${html}</div></div>`, user, { path, description });
}

export function sources(user: User | null) {
  return legal(user, "Sources and licences", "/sources", `
<p class="lead">The data comes from public sources. We process it and keep a reference to where each record came from.</p>
<h2>EDINET (Financial Services Agency of Japan)</h2>
<p>Annual securities reports, half-year securities reports, large shareholding reports and filing metadata, fetched through the EDINET API. Used under the Public Data License 1.0 with this attribution: “Source: EDINET (Financial Services Agency of Japan); processed by japanfilings.” Documents are read through the API only, never by scraping the EDINET website, and requests are paced to keep the load light.</p>
<h2>SEC Form N-PORT data sets</h2>
<p>U.S. registered funds' holdings of Japanese stocks, from the quarterly data sets the U.S. Securities and Exchange Commission publishes. Only each fund's latest report in the quarter is used. Values are in U.S. dollars as filed.</p>
<h2>OpenFIGI</h2>
<p>Financial Instrument Global Identifiers for each ticker, and the ISIN-to-ticker mapping for fund holdings.</p>
<h2>What we change</h2>
<ul><li>Japanese labels become English field names. English company names come from the EDINET code list and each company's own filings.</li><li>Ownership changes, threshold crossings and statements that a filer may make proposals are derived from the filings by rules.</li><li>Cross-shareholdings are matched to tickers by company name; renamed or delisted companies may stay unmatched.</li></ul>
<h2>What we leave out</h2>
<p>We do not collect birth dates, home addresses or phone numbers. Filers and holders in ownership data who are not recognisably organisations are shown as <code>individual filer</code> or <code>individual holder</code>; the source filing stays linked. Officers appear by title only, and meeting candidates are not named. Names of natural persons in filing text are replaced with [氏名省略] where we can recognise them. To ask for a correction or a stop, see <a href="/contact">Corrections and removal</a>.</p>`, "Where japanfilings data comes from: EDINET, SEC Form N-PORT and OpenFIGI, and what we change or leave out.");
}

const CONTACT_LINK = `<a href="/contact">Corrections and removal</a>`;

export function terms(user: User | null, contact: string | null) {
  return legal(user, "Terms of use", "/terms", `
<p>These terms apply to the japanfilings website, API and MCP server (the service). By creating an account or using an API key you accept them. They take effect on 2026-11-01.</p>
<h2>Who runs the service</h2><p>The service is run by an individual business operator. The operator's name and address are given without delay on request, through the contact below. Paid plans are not on sale; the seller will be named here before they are.</p>
<h2>The service</h2><p>japanfilings provides data processed from public filings and other public sources. It is reference information, not investment, legal or tax advice, and not a recommendation about any security. Check important figures against the original source linked from each record.</p>
<h2>Not advice</h2><p>We do not advise on anyone's individual investment decisions and we do not say what to buy, sell or hold. We publish no share prices. Figures, percentiles and sorted lists are arithmetic on the data. If you use the service to give investment advice, to solicit transactions or in any regulated activity, you are responsible for that activity and for any registration it needs.</p>
<h2>Accuracy and liability</h2><p>Filings can contain errors, and our processing can too. The data is provided "as is", without any warranty of accuracy, completeness or availability. To the extent the law allows, we are not liable for losses arising from its use. Nothing here limits liability that the law does not allow to be limited. In particular, where you are a consumer, liability for our intent or gross negligence is not excluded, and any other limit applies only as far as the Consumer Contract Act of Japan permits.</p>
<h2>Your account and key</h2><p>Keep your API key secret. You are responsible for requests made with it. Replace it from the dashboard if it leaks. One person or organisation per account.</p>
<h2>Acceptable use</h2><ul><li>Stay within your plan's monthly and per-minute limits; do not create extra accounts or keys to get around them.</li>
<li>Keep the source attribution when you show the data to others, including the EDINET credit as shown on <a href="/sources">Sources and licences</a>.</li>
<li>Do not try to identify anyone shown as an individual filer or individual holder, and do not use the data to track, profile or harass a person.</li>
<li>Do not use the service in breach of the law or of a data source's terms.</li></ul>
<h2>Redistribution</h2><p>"Raw data" means records from the service, in bulk or substantially whole, in any format. You may use the data in your own analysis and products, show it to your own customers in limited amounts as part of something you add value to, and let your AI products cite or display it with attribution. You may not offer raw data as a feed, a database or an API, to your customers or anyone else.</p>
<h2>Corrections and stops</h2><p>Anyone, account holder or not, can ask us to correct a record, or to stop showing information about a person. Use ${CONTACT_LINK}. We check the request against the source filing and reply by email.</p>
<h2>Plans and payment</h2><p>Every plan is paid; an account without a plan cannot use the API. Billing is not connected yet and nothing is charged. Before any paid plan goes on sale, we will update these terms with how payment, renewal and cancellation work.</p>
<h2>Availability</h2><p>There is no service level commitment. We may suspend the service for maintenance, faults or legal reasons. A data source can change or stop, and a dataset that depends on it can then stop too, without compensation.</p>
<h2>Ending</h2><p>You may stop at any time and ask us to delete your account. We may suspend or end an account that breaches these terms. You may not use the service if you are subject to sanctions that prohibit us from dealing with you.</p>
<h2>Rights</h2><p>Rights in source documents stay with their owners. Rights in our software, site and processing stay with the operator. You receive a right to use the service under these terms, nothing more.</p>
<h2>Changes</h2><p>These terms are standard terms. We may change them where the change benefits users, or is reasonable and fits the purpose of the agreement. We announce the change, its content and its effective date on this page at least 14 days ahead, unless the law or an urgent matter requires less.</p>
<h2>Law, courts, language</h2><p>Japanese law governs these terms. The courts of Japan have jurisdiction, subject to any mandatory right a consumer has to use their own courts. If a translation differs, the English text prevails. If one clause is invalid, the others stand.</p>
<h2>Contact</h2><p>Use the <a href="/contact">contact form</a>.</p>`, "Terms of use for the japanfilings website, API and MCP server.");
}

export function privacy(user: User | null, contact: string | null) {
  return legal(user, "Privacy", "/privacy", `
<p>We collect only the data listed on this page. This page covers people with an account, visitors without one (see Who else handles data and Cookies), and people named in public filings.</p>
<h2>Who is responsible</h2><p>An individual business operator runs the service. The operator's name and address are given without delay on request, through the contact below.</p>
<h2>Account holders: what we keep and why</h2><ul><li>Your email address, to sign you in with one-time links and to contact you about your account.</li><li>A one-way hash of your API key and of your session token, never the values themselves, to authenticate you.</li><li>A monthly count of your API requests, to apply your plan's limit.</li><li>For webhooks: the URL you register, its signing secret and a delivery status, to deliver what you asked for.</li></ul>
<h2>People named in filings</h2><p>Our source documents are public filings on EDINET. Some name individuals. We use them to provide structured data about listed companies and their ownership.</p>
<ul><li>Not shown: individuals who file 5% reports or appear as holders in them, and major shareholders not recognisably organisations. They appear as <code>individual filer</code> or <code>individual holder</code>. Their holdings are not searchable by person, and a link to the source filing remains.</li>
<li>Not shown: the names of officers, representatives and meeting candidates. Officers appear by title only, and voting results by proposal, without the candidate's name.</li>
<li>Issuer names on 5% filings are replaced with the listed-company master name before publication, so a representative's title or name appended to that field is not shown.</li>
<li>Not collected or published as data: birth dates, home addresses and phone numbers. If one appears in a quoted sentence, tell us.</li>
<li>Names in short quotes are removed where we can recognise them. If one gets through, tell us.</li></ul>
<p>You can ask us to correct this or stop showing it: <a href="/contact">Corrections and removal</a>.</p>
<h2>Your requests</h2><p>You can ask us to disclose, correct, delete or stop using personal data we hold about you, or to stop providing it to third parties. Write to the contact below, saying what the request concerns. We may ask for just enough to confirm it is you, reply by email without delay and charge nothing.</p>
<h2>Who else handles data</h2><p>We do not sell data. There are no advertising or tracking cookies and no analytics scripts. These providers process data for us, some outside Japan, under their own safeguards:</p>
<ul><li>Cloudflare, Inc. (United States): hosting and database.</li><li>Resend (United States): sends sign-in emails, so it receives your email address.</li><li>Google Fonts (United States): pages load fonts from Google, so your browser sends its IP address and user agent.</li><li>Paddle.com Market Limited (United Kingdom): would handle payment if paid plans open. It receives nothing while billing is off.</li></ul>
<p>Those countries' laws can differ from Japan's. Ask us for more about a provider's safeguards.</p>
<h2>Security</h2><p>Keys and session tokens are stored only as hashes, traffic uses HTTPS, secrets are kept outside the code, and access to the systems is limited to the operator.</p>
<h2>Keeping and deleting</h2><p>Account data stays while your account exists. Ask us to delete the account and we remove its email, keys, usage and webhooks. Sign-in links expire in 20 minutes. Requests sent through the contact form (what you wrote, the page and your email address) are kept for one year to answer and track them, then deleted; your network address is stored only as a hash that changes every day, to limit abuse.</p>
<h2>Cookies</h2><p>One cookie, <code>jf_session</code>, keeps you signed in for up to 30 days. It is removed when you sign out.</p>
<h2>EU and UK</h2><p>We have not yet reviewed this policy against the GDPR or UK GDPR, and the service is not aimed at people in those regions until we have. You can still make any request above.</p>
<h2>Contact</h2><p>Use the <a href="/contact">contact form</a>.</p>`, "What japanfilings keeps about account holders and people named in filings, who processes it, and how to ask for a correction or a stop.");
}

export function contactPage(user: User | null, state: { sent?: boolean; error?: string; form?: Record<string, string> } = {}) {
  const f = state.form ?? {};
  const kinds: [string, string, string][] = [
    ["correction", "Correct a record", "For example a wrong name match, investor grouping or figure."],
    ["stop", "Stop showing information about a person", "If a record shows you, or someone you represent. We stop showing it while we check."],
    ["personal_data", "Your own account or personal data", "Disclosure, correction, deletion or stopping use."],
    ["other", "Something else", "Questions about the service."],
  ];
  const sel = f.kind ?? "correction";
  return legal(user, "Contact, corrections and removal", "/contact", `
<p>Anyone can use this form, with or without an account. Requests are read by a person and answered by email. Include no more personal information than the request needs.</p>
${state.sent ? `<p class="notice">Thank you. We received your request and will answer by email.</p>` : `
${state.error ? `<p class="notice warn small">${esc(state.error)}</p>` : ""}
<form method="post" action="/contact" class="panel stack" style="gap:14px;max-width:640px">
<fieldset style="border:0;padding:0;margin:0" class="stack"><legend class="small" style="font-weight:600;margin-bottom:6px">What is this about?</legend>
${kinds.map(([v, t, d]) => `<label class="small" style="display:block"><input type="radio" name="kind" value="${v}"${v === sel ? " checked" : ""} required> <strong>${esc(t)}</strong><br><span class="muted">${esc(d)}</span></label>`).join("")}
</fieldset>
<label class="small" style="display:block">Page or API path, if any<br><input name="page" maxlength="300" value="${esc(f.page ?? "")}" placeholder="/companies/7203" style="${INPUT}"></label>
<label class="small" style="display:block">Your request<br><textarea name="message" required minlength="10" maxlength="5000" rows="7" style="${INPUT};resize:vertical">${esc(f.message ?? "")}</textarea></label>
<label class="small" style="display:block">Email for our answer<br><input type="email" name="email" required autocomplete="email" value="${esc(f.email ?? "")}" style="${INPUT}"></label>
<div style="position:absolute;left:-9999px" aria-hidden="true"><label>Leave this empty <input name="website" tabindex="-1" autocomplete="off"></label></div>
<p class="faint xs">We keep requests for one year, then delete them. See the <a href="/privacy">privacy policy</a>.</p>
<div><button class="btn" type="submit">Send request</button></div>
</form>`}`, "Contact japanfilings, or ask us to correct a record or stop showing information about a person.");
}
const INPUT = "width:100%;min-width:0;font:15px var(--body);padding:10px 12px;margin-top:4px;border:1px solid var(--line);border-radius:var(--r);background:var(--surface);color:var(--ink)";

// ---------- Legal notice (特定商取引法に基づく表記) ----------

export type LegalEnv = { LEGAL_PAGE_ENABLED?: string; LEGAL_PROVIDER_NAME?: string; LEGAL_START_OF_SERVICE?: string; LEGAL_CANCELLATION?: string; LEGAL_REFUNDS?: string; CONTACT_EMAIL?: string };
const LEGAL_BLANKS = ["LEGAL_PROVIDER_NAME", "LEGAL_START_OF_SERVICE", "LEGAL_CANCELLATION", "LEGAL_REFUNDS"] as const;

/** Off unless switched on and every blank is filled, so an unfinished notice can never be shown. */
export const legalReady = (env: LegalEnv) => env.LEGAL_PAGE_ENABLED === "true" && LEGAL_BLANKS.every((k) => !!env[k]?.trim());

export function legalPage(user: User | null, env: LegalEnv) {
  const rows: [string, string][] = [
    ["販売者 / Seller", "Paddle.com Market Limited（日本向け）。購入にはPaddleの規約が適用されます。"],
    ["サービス提供者 / Service provider", esc(env.LEGAL_PROVIDER_NAME)],
    ["氏名・住所・電話番号 / Name, address, phone", `請求があれば遅滞なく提供します。請求は<a href="/contact">お問い合わせフォーム</a>からお送りください。`],
    ["価格 / Price", `<a href="/pricing">料金ページ</a>のとおり。税の扱いはチェックアウトに表示します。`],
    ["支払時期・方法 / Payment", "Paddleのチェックアウトで指定した方法により、申込時と各更新時に支払います。"],
    ["提供開始時期 / Start of service", esc(env.LEGAL_START_OF_SERVICE)],
    ["解約 / Cancellation", esc(env.LEGAL_CANCELLATION)],
    ["返金 / Refunds", esc(env.LEGAL_REFUNDS)],
    ["動作環境 / Requirements", "インターネット接続と、HTTPSでAPIまたはMCPを呼び出せる環境。"],
  ];
  return legal(user, "特定商取引法に基づく表記", "/legal", `<div class="panel scroll"><table>${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("")}</table></div>`, "特定商取引法に基づく表記 / Legal notice.");
}

export function notFoundPage(user: User | null, what = "page") {
  const isPage = what === "page";
  return page("Not found | japanfilings", `<div class="wrap"><div class="co-head"><span class="eyebrow">404</span><h1>We could not find that ${esc(what)}.</h1>
<p class="lead">${what === "investor" ? "Filers appear here once they have a 5% filing on file. Look for them on a company page." : isPage ? "The page may have been moved or removed, or the address may be misspelled. Search for a company, or start from one of the pages below." : "Check the ticker (four characters, such as 7203 or 648A) or search by name."}</p>
<form class="field" action="/search" method="get"><input name="q" type="search" placeholder="Ticker or company name" aria-label="Ticker or company name"><button class="btn" type="submit">Search</button></form>
<p class="small"><a href="/">Home</a> · <a href="/events">Ownership changes</a> · <a href="/docs">Documentation</a> · <a href="/pricing">Pricing</a></p></div></div>`, user, { noindex: true });
}

// ---------- Account ----------

export function loginPage(user: User | null, msg = "", devLink?: string) {
  return page("Sign in | japanfilings", `<div class="wrap"><div class="co-head" style="max-width:560px">
<h1>Sign in or create an account</h1>
<p class="lead">We email you a one-time link. There is no password, and no card.</p>
<form method="post" action="/login" class="field" style="margin-top:8px"><label for="email" style="position:absolute;left:-9999px">Email</label><input id="email" type="email" name="email" required placeholder="you@company.com" autocomplete="email"><button class="btn" type="submit">Email me a link</button></form>
${msg ? `<p class="notice small">${esc(msg)}</p>` : ""}${devLink ? `<p class="notice warn small">Development mode, email is not configured: <a href="${esc(devLink)}">sign-in link</a></p>` : ""}
<p class="faint xs">Every plan is paid, from $9 a month; plans are not on sale yet. By signing in you accept the <a href="/terms">terms of use</a> and <a href="/privacy">privacy policy</a>.</p>
</div></div>`, user, { noindex: true });
}

export function dashboardPage(user: User, status: { key_prefix: string; created_at: string; requests: number } | null, newKey?: string, hooks: any[] = [], origin = "", billing?: BillingView) {
  const quota = QUOTA[user.plan];
  const used = status?.requests ?? 0;
  return page("Dashboard | japanfilings", `<div class="wrap">
<div class="co-head"><span class="eyebrow">Signed in as ${esc(user.email)}</span><h1>Dashboard</h1></div>
${newKey ? `<div class="notice stack" style="gap:8px;margin-bottom:24px"><b>Your new API key</b><span class="small">Copy it now. We store only a hash and cannot show it again.</span><pre>${esc(newKey)}</pre></div>` : ""}
<dl class="kv" style="margin:0 0 24px">
<div><dt>Plan</dt><dd style="text-transform:capitalize">${esc(user.plan)}</dd></div>
<div><dt>Requests this month</dt><dd>${fmtNum(used)} <small>of ${fmtNum(quota)}</small></dd></div>
<div><dt>Webhooks</dt><dd>${fmtNum(hooks.length)}</dd></div>
</dl>
<section class="first"><div class="section-head"><h2>API key</h2><a class="small" href="/docs#start">Quick start</a></div>
${status ? `<p class="small" style="margin-bottom:12px"><code>${esc(status.key_prefix)}…</code> created ${esc(status.created_at.slice(0, 10))}</p>` : `<p class="muted" style="margin-bottom:12px">You have no key yet.</p>`}
<form method="post" action="/dashboard/key"><button class="btn${status ? " ghost" : ""}" type="submit">${status ? "Replace key" : "Create key"}</button></form>
${status ? `<p class="faint xs" style="margin-top:8px">Replacing revokes the old key immediately.</p>` : ""}
<pre style="margin-top:16px"><code>curl -H "Authorization: Bearer $JAPANFILINGS_KEY" \\
  "${esc(origin)}/v1/companies/7203"</code></pre></section>
<section><div class="section-head"><h2>Webhooks</h2><a class="small" href="/docs#webhooks">How webhooks work</a></div>
${hooks.length ? `<div class="panel scroll"><table><tr><th>URL</th><th>Sends</th><th class="num">Delivered</th><th>Status</th></tr>
${hooks.map((h) => `<tr><td><code>${esc(h.url)}</code><div class="xs faint mono">${esc(h.id)}</div></td><td class="small">${esc((h.types ?? ["all changes"]).map((t: string) => EVENT_LABEL[t] ?? t).join(", "))}${h.tickers ? `<div class="xs faint">${esc(h.tickers.join(", "))}</div>` : ""}</td><td class="num">${fmtNum(h.delivered_events)}</td><td class="small">${h.enabled ? (h.last_error ? `<span class="chip seal">retrying</span> ${esc(h.last_error)}` : `<span class="chip accent">active</span>`) : `<span class="chip seal">paused</span> after repeated failures`}</td></tr>`).join("")}</table></div>`
  : `<p class="muted">No webhooks yet. They are created with <code>POST /v1/webhooks</code>.</p>`}</section>
<section><h2 style="margin-bottom:10px">Plan</h2><p class="muted" style="margin-bottom:16px">${user.plan === "free" ? (billing?.open ? `You have no plan yet, so your key cannot call the API. <a href="/pricing">Choose a plan</a>, from $9 a month.` : `You have no plan yet, so your key cannot call the API. Plans go on sale at launch. <a href="/pricing">See what they include</a>.`) : `You are on ${user.plan === "starter" ? "Starter" : "Pro"}.`}</p>
${billing?.status === "past_due" ? `<p class="notice small" style="margin-bottom:16px">Your last payment failed and is being retried. Update your payment method to keep this plan.</p>` : ""}
${billing?.cancelAt ? `<p class="small" style="margin-bottom:16px">Your subscription is cancelled and ends on ${esc(billing.cancelAt.slice(0, 10))}. After that your key stops working until you choose a plan again.</p>` : billing?.live && billing.periodEnd ? `<p class="small" style="margin-bottom:16px">Current period ends ${esc(billing.periodEnd.slice(0, 10))}.</p>` : ""}
${billing?.open ? `<div class="row" style="margin-bottom:16px">${billing.live ? "" : buyButton("starter", true, user) + buyButton("pro", true, user)}${billing.customerId ? `<form method="post" action="/billing/portal"><button class="btn ghost" type="submit">Manage billing</button></form>` : ""}</div>` : ""}
<form method="post" action="/logout"><button class="btn ghost" type="submit">Sign out</button></form></section>
</div>`, user, { path: "/dashboard", noindex: true });
}
