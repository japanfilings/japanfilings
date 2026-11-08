// Unit tests for src/webhooks.ts: address filtering, test-send event choice, and registration order.
// Runs on real SQLite with the real migrations; DNS and delivery are stubbed through globalThis.fetch.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "jf-wh-")), "w.mjs");
await build({ entryPoints: [join(here, "../src/webhooks.ts")], bundle: true, format: "esm", outfile: out, platform: "neutral", mainFields: ["module", "main"], logLevel: "error", loader: { ".json": "json" } });
const wh = await import(pathToFileURL(out));

function fakeD1() {
  const s = new DatabaseSync(":memory:");
  for (const f of readdirSync(join(here, "../migrations")).sort()) s.exec(readFileSync(join(here, "../migrations", f), "utf8"));
  const stmt = (sql, args = []) => {
    const q = sql.replace(/\?(\d+)/g, ":p$1");
    const named = () => Object.fromEntries(args.map((v, i) => [`p${i + 1}`, v === undefined ? null : v]).filter(([k]) => new RegExp(`:${k}\\b`).test(q)));
    return {
      sql,
      bind: (...a) => stmt(sql, a),
      first: async () => s.prepare(q).get(named()) ?? null,
      all: async () => ({ results: s.prepare(q).all(named()), meta: {} }),
      run: async () => ({ meta: { changes: Number(s.prepare(q).run(named()).changes) } }),
    };
  };
  return { raw: s, prepare: (sql) => stmt(sql) };
}

test("privateAddress: IPv4 special ranges", () => {
  for (const a of ["0.0.0.0", "10.1.2.3", "127.0.0.1", "169.254.169.254", "172.16.0.1", "172.31.255.255", "192.168.1.1", "100.64.0.1", "192.0.2.1", "198.18.0.1", "203.0.113.5", "224.0.0.1", "255.255.255.255"])
    assert.equal(wh.privateAddress(a), true, a);
  for (const a of ["93.184.216.34", "8.8.8.8", "172.32.0.1", "100.128.0.1"]) assert.equal(wh.privateAddress(a), false, a);
  // Unparseable answers fail closed.
  for (const a of ["", "1.2.3", "256.1.1.1", "0x7f.0.0.1", "example.com", "1:2:3::4::5"]) assert.equal(wh.privateAddress(a), true, a);
});

test("privateAddress: IPv6 in every spelling, including embedded IPv4", () => {
  const blocked = [
    "::", "::1", "0:0:0:0:0:0:0:1", "0000:0000:0000:0000:0000:0000:0000:0001", "[::1]", "::1%lo0",
    "::7f00:1", "::127.0.0.1", "::a9fe:a9fe", // IPv4-compatible ::/96
    "::ffff:7f00:1", "::ffff:127.0.0.1", "0:0:0:0:0:ffff:a9fe:a9fe", "::FFFF:10.0.0.1", // IPv4-mapped
    "::ffff:0:7f00:1", "::ffff:0:169.254.169.254", // IPv4-translated
    "64:ff9b::a9fe:a9fe", "64:ff9b::127.0.0.1", "0064:ff9b:0000:0000:0000:0000:0a00:0001", "64:ff9b:1::808:808", // NAT64
    "2002:7f00:1::", "2002:a9fe:a9fe::1", // 6to4 wrapping private IPv4
    "fc00::1", "fd12:3456::1", "fe80::1", "fe80::1%eth0", "febf::1", "fec0::1", "ff02::1", "ff0e::1", "100::1",
    "2001:db8::1", "2001:0db8:85a3::8a2e:370:7334", "2001::1", "2001:2::1", "3fff::1", "5f00::1", "4000::1",
  ];
  for (const a of blocked) assert.equal(wh.privateAddress(a), true, a);
  for (const a of ["2606:4700:4700::1111", "2001:4860:4860::8888", "2a00:1450:4001:80b::200e", "::ffff:93.184.216.34", "64:ff9b::808:808", "2002:808:808::1"])
    assert.equal(wh.privateAddress(a), false, a);
});

test("parseIPv6 handles ::, leading zeros and dotted tails, and rejects malformed input", () => {
  const hex = (s) => Buffer.from(wh.parseIPv6(s)).toString("hex");
  assert.equal(hex("::1"), "00000000000000000000000000000001");
  assert.equal(hex("0:0:0:0:0:0:0:1"), hex("::1"));
  assert.equal(hex("::ffff:1.2.3.4"), "00000000000000000000ffff01020304");
  assert.equal(hex("1::"), "00010000000000000000000000000000");
  for (const bad of ["1:2:3:4:5:6:7:8:9", "1::2::3", ":::", "1:2:3:4:5:6:7", "12345::", "::1.2.3.256", "g::1", "1:2:3:4:5:6:7:8::"])
    assert.equal(wh.parseIPv6(bad), null, bad);
});

const dns = (address) => Response.json({ Status: 0, Answer: [{ type: address.includes(":") ? 28 : 1, data: address }] });

test("create checks the per-owner limit before any DNS lookup", async () => {
  const db = fakeD1();
  for (let i = 0; i < 5; i++) db.raw.prepare("INSERT INTO webhooks (id, owner, url, secret, created_at, cursor_at) VALUES (?, 'o', 'https://x', 's', '2026', '2026')").run(`wh_${i}`);
  const real = globalThis.fetch;
  let lookups = 0;
  globalThis.fetch = async () => { lookups++; return dns("93.184.216.34"); };
  try {
    const r = await wh.create(db, "o", { url: "https://hook.example/x" }, false);
    assert.match(r.error, /At most 5 webhooks/);
    assert.equal(lookups, 0);
    const ok = await wh.create(db, "p", { url: "https://hook.example/x" }, false);
    assert.ok(ok.id && lookups > 0);
    globalThis.fetch = async () => dns("64:ff9b::a9fe:a9fe");
    assert.match((await wh.create(db, "p", { url: "https://hook.example/x" }, false)).error, /private/);
  } finally { globalThis.fetch = real; }
});

test("sendTest sends the newest event the subscription matches, shaped like cron delivery", async () => {
  const db = fakeD1();
  db.raw.exec("INSERT INTO users (id, email, plan, created_at) VALUES ('o', 'o@example.test', 'pro', '2026')");
  db.raw.prepare("INSERT INTO issuers (edinet_code, ticker, name_ja, name_en, listed) VALUES ('E1', '7203', 'トヨタ自動車株式会社', 'TOYOTA', 1)").run();
  const ev = (id, type, ticker, at) =>
    db.raw.prepare("INSERT INTO ownership_events (event_id, type, issuer_ticker, filer_edinet_code, ingested_at, record) VALUES (?, ?, ?, 'E9', ?, ?)")
      .run(id, type, ticker, at, JSON.stringify({ event_id: id, type, issuer_ticker: ticker, filer_name_ja: "試験アセット株式会社", filer_name_en: "Test Asset" }));
  ev("e_match", "activist_purpose", "7203", "2026-01-01T00:00:00Z");
  ev("e_other_type", "new_stake", "7203", "2026-02-01T00:00:00Z");
  ev("e_other_ticker", "activist_purpose", "9999", "2026-03-01T00:00:00Z");
  const hook = (id, types, tickers) =>
    db.raw.prepare("INSERT INTO webhooks (id, owner, url, secret, types, tickers, created_at, cursor_at) VALUES (?, 'o', 'https://hook.example/x', 's', ?, ?, '2026', '2026')")
      .run(id, types && JSON.stringify(types), tickers && JSON.stringify(tickers));
  hook("wh_f", ["stated_may_make_proposals"], ["7203"]);
  hook("wh_all", null, null);
  hook("wh_filing", ["filing"], null);
  const sent = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (u, init) => {
    if (String(u).startsWith("https://cloudflare-dns.com/")) return dns("93.184.216.34");
    sent.push(JSON.parse(init.body));
    return new Response("ok");
  };
  try {
    assert.deepEqual(await wh.sendTest(db, "o", "wh_f"), { status: 200, ok: true });
    assert.deepEqual(await wh.sendTest(db, "o", "wh_all"), { status: 200, ok: true });
    assert.deepEqual(await wh.sendTest(db, "o", "wh_filing"), { status: 200, ok: true });
    assert.equal(await wh.sendTest(db, "someone_else", "wh_f"), null);
  } finally { globalThis.fetch = real; }
  const [filtered, all, filingOnly] = sent;
  assert.equal(filtered.events.length, 1);
  const e = filtered.events[0];
  assert.equal(e.event_id, "e_match");
  assert.equal(e.type, "stated_may_make_proposals"); // public name, as cron sends it
  assert.equal(e.ingested_at, "2026-01-01T00:00:00Z"); // cron adds ingested_at
  assert.equal(e.test, true);
  assert.equal(all.events[0].event_id, "e_other_ticker");
  assert.deepEqual(filingOnly.events, []); // disclosure-only subscriptions get no ownership event
});

test("checkUrl refuses local names, also with a trailing dot, and IP literals", () => {
  for (const u of ["https://localhost./x", "https://LOCALHOST/x", "https://a.localhost/x", "https://metadata./x", "https://printer.local./x", "https://x.internal/x", "https://nas.home.arpa/x",
    "https://127.0.0.1/x", "https://2130706433/x", "https://0x7f.1/x", "https://[::1]/x", "https://[::ffff:127.0.0.1]/x", "http://hook.example/x"])
    assert.ok(wh.checkUrl(u), u);
  assert.equal(wh.checkUrl("https://hook.example/x"), null);
});

test("create holds the 5-per-account limit under concurrent requests", async () => {
  const db = fakeD1();
  const real = globalThis.fetch;
  globalThis.fetch = async () => dns("93.184.216.34");
  try {
    const results = await Promise.all(Array.from({ length: 8 }, () => wh.create(db, "o", { url: "https://hook.example/x" }, false)));
    assert.equal(results.filter((r) => r.id).length, 5);
    assert.equal(db.raw.prepare("SELECT COUNT(*) AS n FROM webhooks WHERE owner = 'o'").get().n, 5);
  } finally { globalThis.fetch = real; }
});

test("every paid plan receives deliveries, account or key owned; no plan and revoked keys receive nothing", async () => {
  const db = fakeD1();
  db.raw.exec(`INSERT INTO users (id, email, plan, created_at) VALUES ('u_starter','s@example.test','starter','2026'), ('u_pro','p@example.test','pro','2026'), ('u_business','b@example.test','business','2026'), ('u_none','n@example.test','free','2026')`);
  db.raw.exec(`INSERT INTO api_keys (key_hash, plan, created_at, revoked) VALUES ('kpaid','starter','2026',0), ('kfree','free','2026',0), ('krevoked','pro','2026',1)`);
  db.raw.prepare("INSERT INTO ownership_events (event_id, type, issuer_ticker, filer_edinet_code, ingested_at, record) VALUES ('e1', 'new_stake', '7203', 'E9', '2026-06-01T00:00:00Z', ?)")
    .run(JSON.stringify({ event_id: "e1", type: "new_stake", issuer_ticker: "7203", filer_name_en: "Test Asset" }));
  const owners = ["u_starter", "u_pro", "u_business", "u_none", "key:kpaid", "key:kfree", "key:krevoked", "u_missing"];
  owners.forEach((o, i) => db.raw.prepare("INSERT INTO webhooks (id, owner, url, secret, created_at, cursor_at) VALUES (?, ?, ?, 's', '2026', '2026-01-01T00:00:00Z')").run(`wh_${i}`, o, `https://hook.example/${o}`));
  const sent = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (u) => {
    if (String(u).startsWith("https://cloudflare-dns.com/")) return dns("93.184.216.34");
    sent.push(new URL(String(u)).pathname.slice(1));
    return new Response("ok");
  };
  try {
    await wh.deliverAll(db);
    assert.deepEqual(sent.sort(), ["key:kpaid", "u_business", "u_pro", "u_starter"]);
    assert.ok(await wh.sendTest(db, "key:kpaid", "wh_4"));
    assert.equal(await wh.sendTest(db, "u_none", "wh_3"), null);
    assert.equal(await wh.sendTest(db, "key:kfree", "wh_5"), null);
    // Losing the plan stops delivery on the next run.
    db.raw.exec("INSERT INTO ownership_events (event_id, type, issuer_ticker, filer_edinet_code, ingested_at, record) VALUES ('e2', 'new_stake', '7203', 'E9', '2026-06-02T00:00:00Z', '{\"event_id\":\"e2\",\"type\":\"new_stake\"}')");
    db.raw.exec("UPDATE users SET plan = 'free' WHERE id = 'u_pro'");
    sent.length = 0;
    await wh.deliverAll(db);
    assert.deepEqual(sent.sort(), ["key:kpaid", "u_business", "u_starter"]);
  } finally { globalThis.fetch = real; }
});
