// One place for what may be shown about the people behind filings. Every route, webhook and page goes through these.
//
// The rule fails closed. A filer's name is shown only when it is not in investor_excluded_filers and is either known as
// an organisation (in an investor group, or a listed company) or looks like one. A new filer not yet classified by the
// ingest therefore starts hidden. The source filing stays linked through doc_id and source_url.

export const INDIVIDUAL = "individual filer";
export const INDIVIDUAL_HOLDER = "individual holder";
const OMITTED = "[氏名省略]";
export const QUOTE_MAX = 200;

// Same words as _ORG_JA and _ORG_EN in japanfilings/investors.py. Keep the two in step.
const ORG_JA = /株式会社|\(株\)|㈱|有限会社|㈲|合同会社|合資会社|合名会社|相互会社|\(相\)|\(有\)|法人|銀行|信託|証券|保険|生命|金庫|信用組合|組合|共済|財団|基金|協会|機構|持株会|投信|投資|アセット|キャピタル|マネジメント|マネージメント|ファンド|ホールディングス|インベストメント|インベスターズ|パートナーズ|アドバイザーズ|リミテッド|エルエルシー|エルエルピー|エルピー|ピーティーイー|ピーエルシー|インク|カンパニー|コーポレーション|コープ|ベンチャーズ|有限公司|エルティーディー|ピーティーワイ|トラスト|バンク|グループ|商事|商会|工業|製作所|不動産|興産|物産|建設|電機|電気|鉄道|新聞|放送|大学|学園|政府|会社|社団|商店|運輸|倉庫|製薬|化学|産業|ビー・?[ヴブ]ィ?ー?|ビー・?ブイ|エル・?ピー|アーゲー|エヌ・?[ヴブ]イ|ゲーエムベーハ|ゲゼルシャフト|エス・?エ[ーイ]|エス・?アー・?エール・?エル|エス・?ピー・?エ[ーイ]|ソシエテ|リミティッド|エ[ーイ]・ジー|・エービー/;
const ORG_EN = /\b(LTD|LIMITED|LLC|L\.L\.C|LP|L\.P|LLP|PLC|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|FUNDS?|TRUST|BANK|CAPITAL|PARTNERS|MANAGEMENT|INVESTMENTS?|HOLDINGS?|ASSET|ASSETS|ADVISORS?|ADVISERS?|GROUP|PTE|GMBH|AG|SA|S\.A|NV|BV|GOVERNMENT|AUTHORITY|FOUNDATION|PENSION|SECURITIES|PORTFOLIOS?|ETF|SERIES|SPC|UCITS|RETIREMENT|LIMITADA|INSURANCE|ASSURANCE|UNIVERSITY|ENDOWMENT|CLIENTS?|ACCOUNT|NOMINEES?)\b/i;
// Same as _OFFICER, _SUFFIX_THEN_NAME and _TITLE_WORDS in japanfilings/investors.py. Keep the two in step.
// Titles that never occur inside an organisation's name; longer spellings first (破産管財人 before 管財人).
const TITLES_JA = "最高経営責任者|代表取締役|代表執行役|代表社員|代表理事|代表清算人|代表者|業務執行社員|業務執行組合員|業務執行者|無限責任組合員|職務執行者|会計参与|破産管財人|更生管財人|管財人|清算人|支配人|理事長|理事|評議員|監事|会長|取締役|執行役|支店長";
const OFFICER = new RegExp(
  `[\\s,/&]*(${TITLES_JA}|(?<=[\\s,、]|株式会社|有限会社|合同会社)(?:社長|頭取)|(?<=[A-Z]{3})(?:CEO|CFO|COO)(?=\\s+(?!(?:INC|LTD|LLC|CORP|CO|PLC|LIMITED)\\b)[A-Z])|\\b(?:the\\s+)?(?:REPRESENTATIVE|DIRECTOR|PRESIDENT|CEO|COO|CFO|CHAIRMAN|CHAIRPERSON|CHIEF)\\b|\\bEXECUTIVE\\s+(?:DIRECTOR|OFFICER)\\b|\\bMANAGING\\s+(?:DIRECTOR|PARTNER|MEMBER)\\b|\\bGENERAL\\s+MANAGER\\b).*$`,
  "is",
);
const SUFFIX_THEN_NAME = /^(\S.*[\s,]+(?:CO\.?,?\s*LTD\.?|LTD\.?|LIMITED|INC\.?|INCORPORATED|CORPORATION|CORP\.?|L\.?L\.?C\.?|G\.?K\.?|K\.?K\.?|L\.?L\.?P\.?|L\.?P\.?|PLC|PTE\.?\s*LTD\.?|GMBH|AG|S\.A\.?|B\.V\.?|N\.V\.?))[\s,]+[A-Z][A-Z.'\-]*(?:\s+[A-Z][A-Z.'\-]*){0,2}[\s,]*$/i;
const TITLE_WORDS = new RegExp(
  `^(?:[\\s,/&・]+|and\\b|the\\b|of\\b|執行役員|${TITLES_JA}|社長|兼|(?:REPRESENTATIVE|DIRECTOR|PRESIDENT|CEO|COO|CFO|CHAIRMAN|CHAIRPERSON|CHIEF|EXECUTIVE|OFFICER|MANAGING|GENERAL|MANAGER|PARTNER|MEMBER)\\b)+`,
  "i",
);
// A legal form written before the name ("株式会社〇〇"), after a space: "山田太郎 株式会社〇〇代表取締役" puts the person first.
const LEGAL_FORM_FIRST = /\s(?:株式会社|有限会社|合同会社|合資会社|合名会社|(?:一般|公益)(?:社団|財団)法人|学校法人|医療法人|社会福祉法人|宗教法人|特定非営利活動法人|NPO法人|\(株\)|㈱|\(有\)|㈲)(?=\S)/;
const count = (s: string, c: string) => s.split(c).length - 1;
// A professional acting for the holder ("弁護士 架 空 弘 典" as the Japanese filer name of a foreign fund). 弁護士法人 is a firm.
const PROFESSION = /^(?:代理人\s*)?(?:弁護士|司法書士|公認会計士|税理士|行政書士|弁理士)(?!法人)\s*(.+)$/;
const LATIN_ONLY = /^[^\u3040-\u30ff\u3400-\u9fff]+$/;
// A Japanese personal name in Latin letters: two or three words, each spelt only in romanised kana ('kasou tarou',
// 'AKANE KAKUU', 'Jinichi Chouno'). 'Hikari Tsushin K.K.' or 'YOUR TURN' are not. Same as looks_like_romanized_person in investors.py.
const ROMAJI = /^(?:(?:[kstpgdbcjfm])(?=[kstpgdbcjfm])|(?:ky|gy|sh|sy|jy|ch|cy|ts|ny|hy|by|py|my|ry|[kgsztdnhbpmyrwfj])?[aiueo]h?|n)+$/i;
// Romanised legal forms and trade words that are no part of a personal name ('House Kousan Kabushikikaisya').
const ROMAJI_ORG = /(?:KAI|GAI)S[HY]A|KABUSHIKI|YUU?GEN|GODO|GOUDOU|KOU?SAN|FUDOU?SAN|SHOTEN|KANKOU?|HOU?JIN|ZAIDAN|KUMIAI|SANGYOU?|BUSSAN|KENSETSU|SEISAKUSHO|KOU?GYOU?/i;
export function looksLikeRomanizedPerson(name: unknown): boolean {
  const words = String(name ?? "").normalize("NFKC").replace(/\s+/g, " ").trim().split(" ");
  return words.length >= 2 && words.length <= 3 && !looksLikeOrganization(words.join(" ")) && words.every((w) => ROMAJI.test(w) && !ROMAJI_ORG.test(w));
}

/** A trailing word sequence after a company suffix that is a person, not a qualifier like 'Japan' or 'Tokyo Branch'. */
const trailingPerson = (t: string) => looksLikeRomanizedPerson(t) || (t.split(" ").length >= 2 && !orgLike(t) && !/\b(?:JAPAN|TOKYO|BRANCH|OFFICE|DIVISION|INTERNATIONAL|GLOBAL|ASIA|PACIFIC)\b/i.test(t));

/** A filer or holder name cut to the organisation: an officer's title and the person named before or after it go.
 * `org` is what may be shown; `people` is what was cut, for removing the same name from the rest of the record. */
export function splitFilerName(value: unknown): { org: string | null; people: string[] } {
  if (value == null) return { org: null, people: [] };
  const s = String(value).normalize("NFKC").replace(/\s+/g, " ").trim();
  // "山田 太郎(株式会社〇〇代表取締役)": the person outside, the company and title in brackets.
  const b = new RegExp(`^([^()]+?)\\s*\\(([^()]+?)\\s*(?:${TITLES_JA}|社長)[^()]*\\)\\s*$`).exec(s);
  if (b && orgLike(b[2]) && !orgLike(b[1])) return { org: b[2].trim(), people: [b[1].trim()] };
  const pro = PROFESSION.exec(s);
  if (pro && !orgLike(pro[1])) return { org: null, people: [pro[1].trim()] };
  const m = OFFICER.exec(s);
  if (!m) return { org: s ? String(value) : null, people: [] };
  let head = s.slice(0, m.index).trim();
  let tail = m[0];
  const people: string[] = [];
  // "株式会社〇〇(代表取締役 山田太郎)": the title opens a bracket after the organisation; the bracket goes with it.
  if (count(head, "(") > count(head, ")")) {
    const i = head.lastIndexOf("(");
    const inner = head.slice(i + 1).trim();
    head = head.slice(0, i).trim();
    if (inner && !orgLike(inner)) people.push(inner);
    const close = tail.indexOf(")");
    if (close >= 0) tail = tail.slice(0, close);
  }
  const after = tail.replace(TITLE_WORDS, "").trim();
  if (after) {
    // What follows a title can be another organisation ("…組合 無限責任組合員 株式会社△△ 代表取締役 山田太郎",
    // "Taro Yamada, President of ABC Co., Ltd."): keep the people in it, never an organisation's name.
    const sub = OFFICER.test(after) ? splitFilerName(after) : { org: after, people: [] };
    if (sub.org && orgLike(sub.org)) {
      if (!orgLike(head)) { if (head) people.push(head); head = sub.org; }
    } else if (sub.org) people.push(sub.org);
    people.push(...sub.people);
  }
  // "山田太郎 株式会社〇〇代表取締役": a person before an organisation whose legal form comes first.
  const f = LEGAL_FORM_FIRST.exec(head);
  if (f) {
    const before = head.slice(0, f.index).trim();
    if (before && !orgLike(before)) { people.push(before); head = head.slice(f.index).trim(); }
  }
  const n = SUFFIX_THEN_NAME.exec(head);
  if (n) {
    const t = head.slice(n[1].length).replace(/^[\s,]+|[\s,]+$/g, "");
    if (trailingPerson(t)) { people.push(t); head = n[1]; }
  }
  head = head.replace(/[\s,、/&]+$/, "").trim();
  return { org: head || null, people: people.filter((p) => p.length >= 2) };
}
export const publicFilerName = (value: unknown) => splitFilerName(value).org;
const orgLike = (v: string) => { const h = v.replace(/[(（][^()（）]*[)）]/g, "").trim() || v; return ORG_JA.test(h) || ORG_EN.test(h); };

export function looksLikeOrganization(name: unknown): boolean {
  const s = (publicFilerName(name) ?? "").normalize("NFKC");  // "ＦＭＲ　ＬＬＣ" is judged as "FMR LLC"
  if (!s) return false;
  // An heir or estate holds as a person, even when a company name follows ("丁野 仁一(相続人)丁野食品工業株式会社").
  if (/相続人|相続財産|遺言執行者|成年後見/.test(String(name))) return false;
  // Judged on the name outside brackets: "甲元根(KOUGEN-KON)(弁護士法人…)" is a person with a firm in brackets.
  const head = s.replace(/[(（][^()（）]*[)）]/g, "").trim() || s;
  return ORG_JA.test(head) || ORG_EN.test(head);
}

/** A shareholder-register name as shown: no standing proxy, no account label that names a person. Null when what is
 * left is no organisation ("山田太郎(常任代理人 〇〇証券株式会社)" is a person whose agent is a broker). */
export function publicRegisterName(value: unknown): string | null {
  const s = (publicFilerName(value) ?? "").replace(/\s*[(（]常任代理人[^()（）]*[)）]/g, "").replace(/\s*[(（][^()（）]*\d{4,}[^()（）]*[)）]/g, "").trim();
  return s && orgLike(s) ? s : null;
}

/** Columns for a query on a filer code: whether it is excluded, and whether it is a known organisation. */
export const filerFlags = (col: string) =>
  `EXISTS(SELECT 1 FROM investor_excluded_filers x WHERE x.edinet_code = ${col}) AS excluded, ` +
  `(EXISTS(SELECT 1 FROM investor_group_members m WHERE m.edinet_code = ${col}) OR EXISTS(SELECT 1 FROM issuers o WHERE o.edinet_code = ${col} AND o.ticker IS NOT NULL)) AS known_org`;

export type Flags = { excluded?: number | boolean | null; known_org?: number | boolean | null; master_issuer_name_ja?: string | null; master_issuer_name_en?: string | null };

// A representative's title, and everything after it. The long titles never occur inside a company name; the short
// ones only count after a space or a company suffix, so 株式会社長栄 keeps its 社長-looking characters.
const ISSUER_SUFFIX = "(?:株式会社|\\(株\\)|㈱|有限会社|合同会社|Inc\\.?|Ltd\\.?|Corporation|Corp\\.?)";
const REP_TITLE = new RegExp(
  "[\\s,、]*(?:代表取締役|取締役|代表執行役|執行役|代表社員|代表理事|代表者" +
    `|(?:(?<=[\\s,、])|(?<=${ISSUER_SUFFIX}))(?:社長|会長|理事長|頭取|CEO\\b|President\\b|Representative Director\\b|Director\\b)).*$`,
  "is",
);

/** Filing text sometimes appends a representative's title and name to the issuer name ("株式会社ハローズ 代表取締役 甲藤 利行").
 * For a listed issuer the company master is authoritative. Otherwise the title and what follows it go, and so does any
 * name being hidden in this record. Kept at the public boundary so REST/CSV, MCP, pages and webhooks all receive the
 * same value while old stored records remain unchanged. issuer_ticker is kept as filed. */
export function publicIssuerName(value: unknown, master: string | null | undefined, names: unknown[] = []): string | null {
  if (master) return master;
  if (value == null) return null;
  const raw = String(value);
  let s = raw.normalize("NFKC");
  const t = REP_TITLE.exec(s);
  let changed = false;
  if (t) { s = s.slice(0, t.index); changed = true; }
  const rx = namesPattern(names);
  if (rx && s.search(rx) >= 0) { s = s.replace(rx, ""); changed = true; }
  if (!changed) return raw;
  s = s.replace(/[\s,、]+$/, "").replace(/^[\s,、]+/, "").replace(/\s{2,}/g, " ");
  return s.length ? s : null;
}

function canonicalIssuer(record: any, flags: Flags, names: unknown[] = []) {
  if (!record || typeof record !== "object") return record;
  const out = { ...record };
  if ("issuer_name_ja" in out || flags.master_issuer_name_ja) out.issuer_name_ja = publicIssuerName(out.issuer_name_ja, flags.master_issuer_name_ja, names);
  if ("issuer_name_en" in out || flags.master_issuer_name_en) out.issuer_name_en = publicIssuerName(out.issuer_name_en, flags.master_issuer_name_en, names);
  return out;
}

/** True when this filer's name and code must not be shown. */
export const filerHidden = (f: Flags, names: unknown[]) => !!f.excluded || (!f.known_org && !names.some(looksLikeOrganization));

/** A filter by filer code must not work for an excluded filer, or the code alone could be used to follow a person. */
export async function filerBlocked(db: D1Database, code: string | undefined | null) {
  if (!code) return false;
  return !!(await db.prepare("SELECT 1 FROM investor_excluded_filers WHERE edinet_code = ?1").bind(code).first());
}

const escapeRx = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const NO_SCRUB = /^(issuer_|target_name|source_url|doc_id|event_id|type$|ticker$)/;

/** One pattern for every spelling of these names, ignoring spaces inside a name; null when there is none. */
function namesPattern(names: unknown[]): RegExp | null {
  const parts = new Set<string>();
  for (const n of names) {
    const s = String(n ?? "").normalize("NFKC").trim();
    if (s.length < 2 || s === INDIVIDUAL || s === INDIVIDUAL_HOLDER) continue;
    parts.add(s);
    // "名前 (English Name)" appears both whole and as either half.
    for (const p of s.split(/[()（）]/)) if (p.trim().length >= 2) parts.add(p.trim());
  }
  if (!parts.size) return null;
  const alt = [...parts].sort((a, b) => b.length - a.length).map((p) => [...p.replace(/\s+/g, "")].map(escapeRx).join("[\\s　]*"));
  return new RegExp(alt.join("|"), "giu");
}

/** Replaces every spelling of these names in every string, ignoring spaces inside a name. */
export function scrubDeep<T>(value: T, names: unknown[], replacement = INDIVIDUAL): T {
  const rx = namesPattern(names);
  if (!rx) return value;
  const walk = (v: any, key = ""): any => {
    if (typeof v === "string") {
      if (NO_SCRUB.test(key)) return v;
      const n = v.normalize("NFKC");
      return n.search(rx) >= 0 ? n.replace(rx, replacement) : v;
    }
    if (Array.isArray(v)) return v.map((x) => walk(x, key));
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x, k)]));
    return v;
  };
  return walk(value);
}

const PRIVATE_KEY = /(?:address|residen|birth|生年月日|住所)/i;
export const dropPrivate = (v: any): any => {
  if (Array.isArray(v)) return v.map(dropPrivate);
  if (!v || typeof v !== "object") return v;
  return Object.fromEntries(Object.entries(v).filter(([k]) => !PRIVATE_KEY.test(k)).map(([k, x]) => [k, dropPrivate(x)]));
};

// ---------- 5% filings ----------

const holderIsPerson = (h: any) => h?.holder_type === "individual" || (h?.holder_type !== "corporation" && !looksLikeOrganization(h?.name));
const sameName = (a: unknown, b: unknown) => !!a && !!b && String(a).normalize("NFKC").replace(/\s+/g, "") === String(b).normalize("NFKC").replace(/\s+/g, "");

/** The holder behind a 5% filing is a person whatever the filer's code says: the issuer filed for a person
 * (filed_by_issuer, whose filer code is the issuer's own and so "known"), or the only holder is an individual. */
export function holdingFilerIsPerson(r: any): boolean {
  if (r?.filer_is_individual === true) return true;
  const holders: any[] = Array.isArray(r?.holders) ? r.holders : [];
  const byIssuer = (r?.data_warnings ?? []).includes?.("filed_by_issuer") || (!!r?.filer_edinet_code && r.filer_edinet_code === r.issuer_edinet_code);
  if (byIssuer) return !holders.length || holders.some(holderIsPerson);
  return holders.length === 1 && holders[0]?.holder_type === "individual";
}

/** Events carry no holders. Ingest flags them (filer_is_individual). For rows derived before that flag a known code
 * is not enough, because the issuer's own code is "known" when it files for a person: the filer is a person when
 * neither name looks like an organisation, or when the issuer filed and the English field names no organisation. */
export function eventFilerIsPerson(r: any, flags: Flags): boolean {
  if (r?.filer_is_individual === true) return true;
  if (r?.filer_is_individual === false) return false;
  if (!looksLikeOrganization(r?.filer_name_ja) && !looksLikeOrganization(r?.filer_name_en)) return true;
  const byIssuer = r?.filed_by_issuer === true || sameName(r?.filer_name_ja, r?.issuer_name_ja) || sameName(r?.filer_name_ja, flags.master_issuer_name_ja);
  return byIssuer && !looksLikeOrganization(r?.filer_name_en);
}

/** Names for a filer that may be shown: titles and people cut, and an English field dropped when what is left
 * reads as a romanised personal name, so 'kasou tarou' for トラストアップ株式会社 gives null and readers fall back to Japanese. */
function orgFilerNames(r: any, out: any, keys: string[]) {
  const people: string[] = [];
  for (const k of keys) {
    if (!(k in out) || out[k] == null) continue;
    const { org, people: p } = splitFilerName(out[k]);
    people.push(...p);
    const en = k !== "filer_name_ja";
    if (en && !(org && LATIN_ONLY.test(org) && !looksLikeRomanizedPerson(org))) {
      if (org && looksLikeRomanizedPerson(org)) people.push(org);
      out[k] = null;
    } else out[k] = org;
  }
  return people;
}

export function publicHolding(record: string | any, flags: Flags) {
  const r = typeof record === "string" ? JSON.parse(record) : record;
  const out = dropPrivate(r);
  const hide = holdingFilerIsPerson(r) || filerHidden(flags, [r.filer_name_ja, r.filer_name_en, r.filer_name_en_filing]);
  const names: unknown[] = [];
  if (hide) {
    names.push(r.filer_name_ja, r.filer_name_en, r.filer_name_en_filing);
    for (const k of ["filer_name_ja", "filer_name_en", "filer_name_en_filing"]) if (k in out) out[k] = INDIVIDUAL;
    out.filer_edinet_code = null;
  } else {
    names.push(...orgFilerNames(r, out, ["filer_name_ja", "filer_name_en", "filer_name_en_filing"]));
  }
  if (Array.isArray(out.holders)) out.holders = out.holders.map((h: any) => {
    if (!holderIsPerson(h)) {
      const { org, people } = splitFilerName(h?.name);
      names.push(...people);
      return org === h?.name ? h : { ...h, name: org };
    }
    names.push(h?.name);
    return { holder_type: h?.holder_type === "individual" ? "individual" : null, holder_type_ja: h?.holder_type_ja ?? null, name: INDIVIDUAL };
  });
  if (Array.isArray(out.trades)) out.trades = out.trades.map((t: any) => {
    if (!hide && looksLikeOrganization(t?.holder)) return t;
    names.push(t?.holder);
    return { ...t, holder: INDIVIDUAL, note_ja: null };
  });
  return scrubFreeText(canonicalIssuer(scrubDeep(out, names), flags, names));
}

// ---------- Ownership events ----------

// Stored rows keep the type they were derived with, so event ids stay stable. Only this name is shown to anyone.
const STORED_PROPOSALS = "activist_purpose";
export const PROPOSALS = "stated_may_make_proposals";
export const publicEventType = (t: string) => (t === STORED_PROPOSALS ? PROPOSALS : t);
export const storedEventType = (t: string) => (t === PROPOSALS ? STORED_PROPOSALS : t);

const SUBJECT_END = / (?:filed a correction|disclosed a |cut holdings|raised holdings|reduced holdings|states it may|changed its stated)/;
/** The summary starts with the filer as filed, so the subject is replaced whatever its spelling. */
function summaryWithSubject(s: unknown, subject: string) {
  const text = String(s ?? "");
  if (text.startsWith("Holdings of ")) {
    const at = text.search(/ in .*? moved (?:above|below) /);
    return at > 0 ? `Holdings of ${subject}${text.slice(at)}` : `Holdings of ${subject} changed.`;
  }
  const m = SUBJECT_END.exec(text);
  return m ? subject + text.slice(m.index) : subject === INDIVIDUAL ? `An ${INDIVIDUAL} filed a 5% report.` : `${subject} filed a 5% report.`;
}
const anonymousSummary = (s: unknown) => summaryWithSubject(s, INDIVIDUAL);

export function publicEvent(record: string | any, flags: Flags) {
  const r = typeof record === "string" ? JSON.parse(record) : record;
  const { activist, ...rest } = r;
  const out: any = { ...rest, type: publicEventType(r.type), stated_may_make_proposals: !!activist };
  if (typeof out.event_id === "string") out.event_id = out.event_id.replace(STORED_PROPOSALS, PROPOSALS);
  delete out.filer_is_individual;
  if (!eventFilerIsPerson(r, flags) && !filerHidden(flags, [r.filer_name_ja, r.filer_name_en])) {
    const people = orgFilerNames(r, out, ["filer_name_ja", "filer_name_en", "filer_name_en_filing"]);
    const subject = out.filer_name_en ?? out.filer_name_ja;
    if (subject && "summary_en" in out) out.summary_en = summaryWithSubject(r.summary_en, subject);
    return scrubFreeText(canonicalIssuer(scrubDeep(out, people), flags, people));
  }
  const names = [r.filer_name_ja, r.filer_name_en, r.filer_name_en_filing];
  out.summary_en = anonymousSummary(r.summary_en);
  out.filer_name_ja = INDIVIDUAL;
  out.filer_name_en = INDIVIDUAL;
  if ("filer_name_en_filing" in out) out.filer_name_en_filing = INDIVIDUAL;
  out.filer_edinet_code = null;
  return scrubFreeText(canonicalIssuer(scrubDeep(out, names), flags, names));
}

// ---------- Trades ----------

/** A holder in a trade table can be a joint holder of an organisation's report, so the holder's own name decides too. */
export const tradeHolderHidden = (flags: Flags, holder: unknown) => !!flags.excluded || !looksLikeOrganization(holder);

// ---------- Filings ----------

export function publicFiling<T extends Record<string, any>>(row: T): Omit<T, "excluded" | "known_org"> {
  const { excluded, known_org, ...rest } = row;
  if (!filerHidden({ excluded, known_org }, [row.filer_name_ja])) {
    const { org, people } = splitFilerName(row.filer_name_ja);
    return scrubFreeText(org === row.filer_name_ja ? rest : scrubDeep({ ...rest, filer_name_ja: org }, people)) as any;
  }
  return scrubDeep({ ...rest, filer_name_ja: INDIVIDUAL, edinet_code: null }, [row.filer_name_ja]) as any;
}

// ---------- Tender offers ----------

export function publicTenderOffer(record: string | any, flags: Flags) {
  const r = typeof record === "string" ? JSON.parse(record) : record;
  const out: any = { ...r };
  const names: unknown[] = [];
  if (filerHidden(flags, [r.filer_name_ja])) {
    names.push(r.filer_name_ja);
    out.filer_name_ja = INDIVIDUAL;
    out.edinet_code = null;
  } else if (r.filer_name_ja) {
    const { org, people } = splitFilerName(r.filer_name_ja);
    out.filer_name_ja = org;
    names.push(...people);
  }
  if (r.filer_is_individual && out.filer_name_ja !== INDIVIDUAL) {
    names.push(r.filer_name_ja);
    out.filer_name_ja = INDIVIDUAL;
    out.edinet_code = null;
  }
  if (r.offeror_name_ja && (r.offeror_is_individual || !looksLikeOrganization(r.offeror_name_ja))) {
    names.push(r.offeror_name_ja);
    out.offeror_name_ja = INDIVIDUAL;
  }
  const clean: any = scrubDeep(out, names);
  for (const k of ["purpose_quote_ja", "board_opinion_quote_ja", "result_quote_ja"]) if (clean[k]) clean[k] = quote(clean[k]);
  return clean;
}

// ---------- Annual-report people ----------

// A representative's title, token by token ("代表取締役社長 社長執行役員 丙々美 勉" gives "代表取締役社長 社長執行役員").
const TITLE_TOKEN = "(?:代表|取締役|執行役員|執行役|社長|会長|副|専務|常務|常勤|社外|兼|頭取|理事長|理事|監査役|監査等委員|委員長|代表社員|業務執行社員|職務執行者|組合長|総裁|学長|所長|院長|最高[一-龥]{0,6}責任者|グループ|CEO|COO|CFO|CIO|CTO|CHRO|CMO|CSO|[()（）・、/]|[一-龥ァ-ヶー]{1,8}(?:担当|本部長|部長|室長|統括|管掌|責任者|支店長))";
const TITLE_ONLY = new RegExp(`^(?:${TITLE_TOKEN})+$`, "u");
const TITLE_PREFIX = new RegExp(`^(?:${TITLE_TOKEN})+`, "u");

/** The title part of a representative field, or null when it cannot be told apart from the name. Never the name. */
export function representativeTitle(value: unknown): string | null {
  const tokens = String(value ?? "").normalize("NFKC").trim().split(/\s+/).filter(Boolean);
  const title: string[] = [];
  let rest = false;
  for (const t of tokens) {
    if (!rest && TITLE_ONLY.test(t)) { title.push(t); continue; }
    if (!rest) {
      // "代表取締役山田太郎": the title is the vocabulary at the start of the token.
      const m = TITLE_PREFIX.exec(t);
      if (m && m[0].length < t.length) title.push(m[0]);
    }
    rest = true;
  }
  return title.length && rest ? title.join(" ") : null;
}

/** Officers by title only, the representative by title only; major shareholders named only when they are recognisably organisations. */
export function publicCompanyPeople<T extends Record<string, any>>(company: T): T {
  const out: any = dropPrivate(company ?? {});
  if ("representative" in out) {
    out.representative_title = representativeTitle(out.representative);
    delete out.representative;
  }
  if (Array.isArray(out.officers)) out.officers = out.officers.map((o: any) => ({ title: o?.title == null ? null : scrubText(o.title) }));
  if (Array.isArray(out.major_shareholders)) out.major_shareholders = out.major_shareholders.map((s: any) => { const name = looksLikeOrganization(s?.name) ? publicRegisterName(s.name) : null; return { ...s, name: name ?? INDIVIDUAL_HOLDER }; });
  return out;
}

// ---------- Quotes and free text ----------
// The same pattern rules as japanfilings/governance.py scrub() and people.py scrub(). The export already masks every
// name it knows; these run again at serve time on every free-text field, as a backstop. Keep the two in step.

const K = "一-龥々\u3400-\u4dbf\uf900-\ufaff";
const KANJI_NAME = "[一-龥々]{1,5}[ 　]?[一-龥々ぁ-んァ-ヶ]{1,5}";
const TITLED = new RegExp("(代表取締役(?:社長|会長)?|取締役(?:社長|会長)?|監査役|執行役員?|理事長|(?<![会本])社長|会長|公認会計士)[ 　:：]*(?!社長|会長|副|兼|及び|および|並びに|以下)" + KANJI_NAME + "(?=は|が|の|に|氏|を|、|。)", "gu");
// A run of titles, a function's head included ("財経担当", "リスクマネジメント担当"), then a name before は/及び/、/解任 ...
const TITLES_EXT = `(?:(?:当社|同社|前|元|現)?(?:代表取締役|代表執行役|取締役|執行役員|執行役|監査役|最高[一-龥]{0,6}責任者|CFO|CEO|COO|CIO|CTO|CHRO|CMO|CSO|(?<![会本])社長|会長|副社長|副会長|専務|常務|兼|理事長|[一-龥ァ-ヶー・]{1,12}?(?:担当|管掌|統括|本部長|部長|室長|責任者))[ 　]?)+`;
const TITLED_EXT = new RegExp(
  `(${TITLES_EXT})(?!社長|会長|副|兼|及び|および|並びに|以下|会|等|として|締役|執行|役|部|者|室|全|各|\\[)([${K}]{1,4}[ 　]?(?:[${K}]|[ぁ-ゖ](?<![のがはをにでともやへ])){1,4})(?=及び|および|並びに|ならびに|は|が|、|。|の(?:解任|選任|再任)|解任|選任|再任|辞任|退任|就任|氏|$)`,
  "gu",
);
const NOT_NAME = /役|会|部|課|室|員|者|職|制|業|監|報|酬|規|程|責|選|候|補|等|経|営|執|統|括|掌|株|件|議|案|額|期|委|任|全|各|数|名|当社|同社|自身|本人|以外|以上|以下|管理|事業|分野|領域|範囲|体制|機能|組織|本部|部門|グループ|財務|経理|総務|人事|法務|広報|企画|技術|開発|生産|販売|営業|品質|購買|調達|情報|内部|監督|審議|方針|工場|支店|支社|本社|営業所|事業所|センター|研究所|拠点/u;
// "<name>氏", kana inside a name allowed ("甲村けん牛氏"); not 同氏, 両氏, 上記両氏, 当該氏名, 摂氏.
const UJI = new RegExp(`([${K}〆ヵヶ]{1,4}(?:[ぁ-ゖァ-ヶー](?<![のがはをにでともやへ])){0,2}[${K}]{0,4})[ 　]?氏(?![名族])`, "gu");
const UJI_AFTER = ["上記", "当該", "前記", "後記", "下記", "両", "各", "同", "諸", "某", "他"];
const UJI_ALONE = new Set(["摂", "華", "本", "当", "貴", "女", "男", "該", "源", "平"]);
const PAIR_UJI = /[一-龥々]{2,6}(?:及び|、|・|と)[一-龥々]{2,6}の?(?:両|各)?氏(?![名族])/gu;
const KATA_UJI = /(?<![ァ-ヶー・])[ァ-ヶー]{2,}(?:[・=＝][ァ-ヶー]{2,})+[ 　]?氏(?![名族])/gu;
const SAMA = /([一-龥々\u3400-\u4dbf]{2,5})[ 　]?様(?![式々相態似])/gu;
const SAMA_STOP = /(?:皆|客|株主|主|同|多|仕|模|異|一|有|態|左|各|貴|神|仏|王|者|方|人|員|先|社|家|殿|上|会|位|生|両|御|奥|若|姫|宮|子供|親|担当|関係|外|内|当|今|此|斯|如何|何|様|[郡町村市県区道都府])$/u;
const ACCOUNTANT = new RegExp("公認会計士(?!等|協会|法|事務所|監査|資格|試験|・)\\s*" + KANJI_NAME + "(?:氏|様)?", "gu");
const TRIGGER = /氏|様|公認会計士|取締役|執行役|社長|会長|担当|管掌|統括|部長|室長|責任者|理事|監査役|CEO|CFO|COO|CIO|CTO/u;

function titledName(all: string, titles: string, name: string): string {
  const cut = /及び?|並びに?|ならびに|および/u.exec(name);
  const rest = cut ? name.slice(cut.index) : "";
  const n = cut ? name.slice(0, cut.index) : name;
  return n.length < 2 || NOT_NAME.test(n) ? all : titles + OMITTED + rest;
}

/** Names removed where they can be recognised, the rest of the text verbatim. */
export function scrubText(text: unknown): string | null {
  if (text == null) return null;
  let s = String(text);
  if (!TRIGGER.test(s)) return s;
  if (s.includes("公認会計士")) s = s.replace(ACCOUNTANT, `公認会計士${OMITTED}`);
  if (s.includes("氏")) {
    s = s.replace(PAIR_UJI, OMITTED);
    s = s.replace(UJI, (all, run: string) => (UJI_AFTER.some((w) => run.endsWith(w)) || UJI_ALONE.has(run) ? all : `${OMITTED}氏`));
    s = s.replace(KATA_UJI, `${OMITTED}氏`);
  }
  if (s.includes("様")) s = s.replace(SAMA, (all, run: string) => (SAMA_STOP.test(run) ? all : `${OMITTED}様`));
  s = s.replace(TITLED_EXT, (all, titles: string, name: string) => titledName(all, titles, name));
  s = s.replace(TITLED, (all, title: string) => {
    const after = all.slice(title.length).replace(/^[ 　:：]*/, "");
    const cut = /解任|選任|再任|辞任|退任|就任|及び|並びに/u.exec(after);
    return title + OMITTED + (cut ? after.slice(cut.index) : "");
  });
  return s;
}

const KEEP_KEYS = /^(?:issuer_name_(?:ja|en)|target_name_ja|source_url|url|doc_id|event_id|type|ticker|.*_code|.*_id|.*_date|.*_at)$/;
/** scrubText on every string in a value, except identifiers, links and dates. */
export function scrubFreeText<T>(value: T): T {
  const walk = (v: any, key = ""): any => {
    if (typeof v === "string") return KEEP_KEYS.test(key) ? v : scrubText(v);
    if (Array.isArray(v)) return v.map((x) => walk(x, key));
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x, k)]));
    return v;
  };
  return walk(value);
}

/** Every quote served: names removed where they can be recognised, and at most 200 characters. */
export function quote(text: unknown): string | null {
  if (text == null) return null;
  const s = scrubText(text) as string;
  return s.length > QUOTE_MAX ? s.slice(0, QUOTE_MAX - 1) + "…" : s;
}

/** A holder cell can list joint holders ("WMパートナーズ株式会社、乙本守祥、甲永康雄"): the people in it are cut. */
export function publicHolderList(value: unknown): string | null {
  if (value == null) return null;
  const s = String(value);
  if (!/[、,，]/.test(s)) return s;
  const parts = s.split(/([、,，]\s*)/);
  let changed = false;
  const out = parts.map((p, i) => {
    if (i % 2 === 1 || !p.trim() || looksLikeOrganization(p)) return p;
    changed = true;
    return INDIVIDUAL_HOLDER;
  });
  return changed ? out.join("") : s;
}
