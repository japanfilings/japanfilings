// Unit tests for the issuer-name rule in src/privacy.ts. Route-level coverage is in masking.test.mjs.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "jf-priv-")), "p.mjs");
await build({ entryPoints: [join(here, "../src/privacy.ts")], bundle: true, format: "esm", outfile: out, platform: "neutral", logLevel: "error" });
const { looksLikeOrganization, publicIssuerName, publicHolding, publicEvent, publicFilerName, publicFiling, publicTenderOffer, publicRegisterName, publicCompanyPeople, splitFilerName } = await import(pathToFileURL(out));

// The issuer name as stored for S100Z6DM, spacing included.
const FILED = "株式会社ハローズ           代表取締役    甲藤    利行";
const PERSON = /代表取締役|甲藤\s*利行/;

test("issuer name: master wins, otherwise the representative's title and name are cut", () => {
  assert.equal(publicIssuerName(FILED, "株式会社ハローズ"), "株式会社ハローズ");
  assert.equal(publicIssuerName(FILED, null), "株式会社ハローズ");
  for (const [filed, want] of [
    ["株式会社サンプル 取締役社長 山田 太郎", "株式会社サンプル"],
    ["サンプル株式会社　代表執行役社長　山田太郎", "サンプル株式会社"],
    ["株式会社サンプル代表取締役山田太郎", "株式会社サンプル"],
    ["株式会社サンプル 社長 山田 太郎", "株式会社サンプル"],
    ["Sample Co., Ltd. Representative Director Taro Yamada", "Sample Co., Ltd."],
    ["Sample Inc. President and CEO Taro Yamada", "Sample Inc."],
  ]) assert.equal(publicIssuerName(filed, null), want, filed);
  // Ordinary names, including ones with 社長/会長-looking characters, are returned exactly as filed.
  for (const name of ["株式会社長栄", "株式会社長府製作所", "株式会社長谷工コーポレーション", "(株)NATTY  SWANKYホールディングス", "HALOWS CO.,LTD.", "東京都競馬株式会社"])
    assert.equal(publicIssuerName(name, null), name);
  // A hidden filer's name goes from the issuer name too, and a name that was only a person becomes null.
  assert.equal(publicIssuerName("株式会社ハローズ 甲藤 利行", null, ["甲藤 利行"]), "株式会社ハローズ");
  assert.equal(publicIssuerName("丙塲 勇佑", null, ["丙塲 勇佑"]), null);
  assert.equal(publicIssuerName(null, null), null);
});

const record = { doc_id: "S100Z6DM", issuer_ticker: "2742", issuer_name_ja: FILED, issuer_name_en: "HALOWS CO.,LTD.", filer_edinet_code: "E07725", filer_name_ja: "甲藤 利行", filer_name_en: "Toshiyuki Kouto", holders: [{ name: "甲藤 利行", holder_type: "individual" }] };

test("S100Z6DM shape: 5% record and event never carry the excluded person's name in issuer fields", () => {
  for (const flags of [{ excluded: 1, master_issuer_name_ja: "株式会社ハローズ" }, { excluded: 1 }, { excluded: 0, known_org: 1 }]) {
    const h = publicHolding(JSON.stringify(record), flags);
    assert.equal(h.issuer_name_ja, "株式会社ハローズ");
    assert.equal(h.issuer_ticker, "2742");
    assert.doesNotMatch(JSON.stringify(h.issuer_name_ja) + JSON.stringify(h.issuer_name_en), PERSON);
    const e = publicEvent({ ...record, type: "stake_increase", event_id: "S100Z6DM:stake_increase", summary_en: "甲藤 利行 raised holdings in HALOWS (2742)." }, flags);
    assert.equal(e.issuer_name_ja, "株式会社ハローズ");
    assert.equal(e.issuer_ticker, "2742");
    if (flags.excluded) assert.doesNotMatch(JSON.stringify(e), PERSON);
  }
});

// Shapes from DATA_CHECK_20261105 重大-1. Every filer here is a known organisation (in a group or a listed issuer).
var ORG = { excluded: 0, known_org: 1 };
const LEAKED = /kasou|tarou|Jiro Kakuu|Otsumura|Jinichi|Kouno Konosuke|AKANE KAKUU|Placeholder|HEIZAKI|Kasouda|Kasoumoto|戊田|浩之|己島|啓修|丙﨑|乙村|康宏|庚井/i;

test("filer names: officer titles and the person before or after them are cut", () => {
  for (const [filed, want] of [
    ["Arcus Investment Limited       Director/Executive Director Edward Placeholder", "Arcus Investment Limited"],
    ["JINYA-CONNECT Co.,Ltd. CEO TOMOKO HEIZAKI", "JINYA-CONNECT Co.,Ltd."],
    ["Kyosan Mizuho Kai Chairman Hirokazu Kasouda", "Kyosan Mizuho Kai"],
    ["KYOCERA CORPORATION Hideo Kasoumoto, Director and President", "KYOCERA CORPORATION"],
    ["RIZAP GROUP, Inc. Takeshi Kakuuno, Representative Director, President", "RIZAP GROUP, Inc."],
    ["Masaki Shoji Co., Ltd.  Representative DIrector  Shiro Kakuuda", "Masaki Shoji Co., Ltd."],
    ["KASOUKAWA G.K. CEO RYOUTAROU KASOUKAWA", "KASOUKAWA G.K."],
    ["Limited Society NSEC CEO Mineo Kasoumori", "Limited Society NSEC"],
    ["ブラックロック・ジャパン株式会社代表取締役社長 戊田 浩之", "ブラックロック・ジャパン株式会社"],
    ["株式会社シティインデックスイレブンス代表取締役 己島啓修", "株式会社シティインデックスイレブンス"],
    ["株式会社 陣屋コネクト 代表取締役 丙﨑 知子", "株式会社 陣屋コネクト"],
    ["不二ラテックス共栄会 理事長 壬中 眞介", "不二ラテックス共栄会"],
    ["住友電気工業株式会社 社長 辛上 治", "住友電気工業株式会社"],
  ]) assert.equal(publicFilerName(filed), want, filed);
  // English fields that are Japanese personal names in romaji are told from company names without a legal form.
  for (const [ja, en, kept] of [["光通信株式会社", "Hikari Tsushin K.K.", true], ["ゴールドマン・サックス・インターナショナル", "Goldman Sachs International", true], ["株式会社YOUR TURN", "YOUR TURN", true],
    ["株式会社AIR-U", "Kouno Konosuke", false], ["株式会社和円商事", "KAKUU TOSHIYUKI", false]]) {
    const h = publicHolding({ doc_id: "D", filer_edinet_code: "E1", filer_name_ja: ja, filer_name_en_filing: en, holders: [{ name: ja, holder_type: "corporation" }] }, ORG);
    assert.equal(h.filer_name_en_filing, kept ? en : null, en);
  }
  for (const name of ["株式会社長栄", "Silchester International Investors LLP", "ＦＭＲ　ＬＬＣ", "野村證券株式会社"]) assert.equal(publicFilerName(name), name);
});

test("organisation filer whose English field is a personal name shows its Japanese name", () => {
  for (const [ja, en, ticker] of [["トラストアップ株式会社", "kasou tarou", "2901"], ["有限会社MASTER   MIND", "Jiro Kakuu", "9983"], ["株式会社惻隠坊", "AKANE KAKUU", "638A"]]) {
    const rec = { doc_id: "D", issuer_ticker: ticker, issuer_name_ja: "株式会社X", filer_edinet_code: "E1", filer_name_ja: ja, filer_name_en_filing: en,
      holders: [{ name: ja, holder_type: "corporation" }, { name: "誰か", holder_type: "individual" }] };
    const h = publicHolding(rec, ORG);
    assert.equal(h.filer_name_ja, ja);
    assert.equal(h.filer_name_en_filing, null);
    assert.equal(h.filer_edinet_code, "E1");
    assert.doesNotMatch(JSON.stringify(h), LEAKED);
    const e = publicEvent({ ...rec, type: "stake_decrease", event_id: "D:stake_decrease", filer_name_en: en, summary_en: `${en} reduced holdings in X (${ticker}) to 5.00% from 6.00%.` }, ORG);
    assert.equal(e.filer_name_en, null);
    assert.equal(e.summary_en, `${ja} reduced holdings in X (${ticker}) to 5.00% from 6.00%.`);
    assert.doesNotMatch(JSON.stringify(e), LEAKED);
  }
});

test("filed_by_issuer: the issuer's own code does not make the person behind the filing an organisation", () => {
  const rec = { doc_id: "S100ZA48", issuer_ticker: "7050", issuer_edinet_code: "E34657", issuer_name_ja: "株式会社フロンティアインターナショナル", issuer_name_en: "FRONTIER INTERNATIONAL INC.",
    filer_edinet_code: "E34657", filer_name_ja: "株式会社フロンティアインターナショナル", filer_name_en: "FRONTIER INTERNATIONAL INC.", filer_name_en_filing: "Yasuhiro Otsumura",
    holders: [{ name: "乙村 康宏", holder_type: "individual", holder_type_ja: "個人" }], data_warnings: ["filed_by_issuer"] };
  const h = publicHolding(rec, ORG);
  assert.equal(h.filer_name_en_filing, "individual filer");
  assert.equal(h.filer_edinet_code, null);
  assert.doesNotMatch(JSON.stringify(h), LEAKED);
  // An event stored before ingest flagged it: the filer is the issuer itself and the English field is no organisation.
  const legacy = { type: "stake_decrease", event_id: "S100ZA48:stake_decrease", doc_id: "S100ZA48", issuer_ticker: "7050", issuer_name_ja: rec.issuer_name_ja, issuer_name_en: rec.issuer_name_en,
    filer_edinet_code: "E34657", filer_name_ja: rec.filer_name_ja, filer_name_en: "Yasuhiro Otsumura", summary_en: "Yasuhiro Otsumura reduced holdings in FRONTIER INTERNATIONAL INC. (7050) to 48.67% from 53.31%." };
  for (const ev of [legacy, { ...legacy, filer_is_individual: true, summary_en: "individual filer reduced holdings in FRONTIER INTERNATIONAL INC. (7050) to 48.67% from 53.31%." }]) {
    const e = publicEvent(ev, ORG);
    assert.equal(e.filer_name_en, "individual filer");
    assert.equal(e.filer_edinet_code, null);
    assert.match(e.summary_en, /^individual filer reduced holdings/);
    assert.doesNotMatch(JSON.stringify(e), LEAKED);
    assert.equal("filer_is_individual" in e, false);
  }
  // 3066 shape: stored before the flag, the person's own name as filer under the issuer's code.
  const otsumi = publicEvent({ type: "stake_increase", event_id: "X:stake_increase", doc_id: "X", issuer_ticker: "3066", filer_edinet_code: "E03500", filer_name_ja: "乙美 司", filer_name_en: null,
    summary_en: "乙美 司 raised holdings in JB ELEVEN CO.,LTD. (3066) to 3.55% from 3.49%." }, ORG);
  assert.equal(otsumi.filer_edinet_code, null);
  assert.doesNotMatch(JSON.stringify(otsumi), /乙美/);
  // 2814 and 9983 shapes: holders listed, sole individual holder.
  const sato = { ...rec, issuer_ticker: "2814", issuer_edinet_code: "E00484", filer_edinet_code: "E00484", filer_name_en_filing: "Jinichi Chouno", holders: [{ name: "丁野 仁一", holder_type: "individual" }, { name: "丁野 ふみ", holder_type: "individual" }] };
  assert.equal(publicHolding(sato, ORG).filer_edinet_code, null);
  assert.doesNotMatch(JSON.stringify(publicHolding(sato, ORG)), /Jinichi|丁野/);
});

test("organisation with title and person in its filer names: the organisation stays, the person goes everywhere", () => {
  const rec = { doc_id: "S1", issuer_ticker: "3697", issuer_name_ja: "株式会社X", filer_edinet_code: "E23032", filer_name_ja: "アーカス・インベストメント・リミテッド",
    filer_name_en_filing: "Arcus Investment Limited       Director/Executive Director Edward Placeholder", holders: [{ name: "アーカス・インベストメント・リミテッド", holder_type: "corporation" }] };
  const h = publicHolding(rec, ORG);
  assert.equal(h.filer_name_en_filing, "Arcus Investment Limited");
  assert.equal(h.filer_edinet_code, "E23032");
  const e = publicEvent({ ...rec, type: "stake_increase", event_id: "S1:stake_increase", filer_name_en: rec.filer_name_en_filing,
    summary_en: "Arcus Investment Limited       Director/Executive Director Edward Placeholder raised holdings in X (3697) to 6.00% from 5.00%." }, ORG);
  assert.equal(e.filer_name_en, "Arcus Investment Limited");
  assert.equal(e.summary_en, "Arcus Investment Limited raised holdings in X (3697) to 6.00% from 5.00%.");
  const t = publicEvent({ ...rec, type: "threshold_crossed", event_id: "S1:threshold_crossed:10", filer_name_ja: "ブラックロック・ジャパン株式会社代表取締役社長 戊田 浩之", filer_name_en: null,
    summary_en: "Holdings of ブラックロック・ジャパン株式会社代表取締役社長 戊田 浩之 in X (3697) moved above 10% (9.00% -> 10.50%)." }, ORG);
  assert.equal(t.filer_name_ja, "ブラックロック・ジャパン株式会社");
  assert.equal(t.summary_en, "Holdings of ブラックロック・ジャパン株式会社 in X (3697) moved above 10% (9.00% -> 10.50%).");
  for (const x of [h, e, t]) assert.doesNotMatch(JSON.stringify(x), LEAKED);
  const f = publicFiling({ doc_id: "F", filer_name_ja: "株式会社シティインデックスイレブンス代表取締役 己島啓修", edinet_code: "E2", excluded: 0, known_org: 1 });
  assert.equal(f.filer_name_ja, "株式会社シティインデックスイレブンス");
});

test("names after titles, in brackets and after a second organisation are cut; organisations stay (DeepSeek review 2026-11-05)", () => {
  const cases = [
    ["Taro Yamada, President of ABC Co., Ltd.", "ABC Co., Ltd."],
    ["山田 太郎(株式会社〇〇代表取締役)", "株式会社〇〇"],
    ["〇〇投資事業有限責任組合 無限責任組合員 株式会社△△ 代表取締役 山田太郎", "〇〇投資事業有限責任組合"],
    ["一般社団法人〇〇 理事 山田太郎", "一般社団法人〇〇"],
    ["〇〇財団 評議員 山田太郎", "〇〇財団"],
    ["株式会社理研", "株式会社理研"],
  ];
  for (const [v, want] of cases) assert.equal(publicFilerName(v), want, v);
  const t = publicTenderOffer({ filer_name_ja: "甲木 剛志", offeror_name_ja: "株式会社A、甲木 剛志", offeror_is_individual: true, filer_is_individual: true, edinet_code: "E9" }, ORG);
  assert.doesNotMatch(JSON.stringify(t), /甲木/);
});

test("round 2 (DeepSeek review 2026-11-05): more titles, bracketed and leading persons, glued CEO, agents", () => {
  for (const [v, want, person] of [
    ["株式会社〇〇 最高経営責任者 山田太郎", "株式会社〇〇", "山田太郎"],
    ["株式会社〇〇 会計参与 山田太郎", "株式会社〇〇", "山田太郎"],
    ["株式会社〇〇 清算人 山田太郎", "株式会社〇〇", "山田太郎"],
    ["〇〇株式会社 破産管財人 山田太郎", "〇〇株式会社", "山田太郎"],
    ["〇〇株式会社 支配人 山田太郎", "〇〇株式会社", "山田太郎"],
    ["合同会社CYGNUS 代表社員 一般社団法人SPE 業務執行者 癸木洋介", "合同会社CYGNUS", "癸木洋介"],
    ["株式会社IDホールディングス 代表取締役社長 兼 グループ最高経営責任者  舩架 真樹", "株式会社IDホールディングス", "舩架 真樹"],
    ["山田太郎 株式会社〇〇代表取締役", "株式会社〇〇", "山田太郎"],
    ["株式会社〇〇(代表取締役 山田太郎)", "株式会社〇〇", "山田太郎"],
    ["株式会社〇〇（代表取締役　山田太郎）", "株式会社〇〇", "山田太郎"],
    ["PT ASIA PLASTIK VIKTORICEO FIKTIF", "PT ASIA PLASTIK VIKTORI", "FIKTIF"],
    ["Francis Capital IncCEO  Ann Fic Tion", "Francis Capital Inc", "Ann Fic Tion"],
    ["弁護士 架 空 弘 典", null, "架 空 弘 典"],
  ]) {
    const r = splitFilerName(v);
    assert.equal(r.org, want, v);
    assert.ok(r.people.includes(person), v + " " + JSON.stringify(r.people));
  }
  // A qualifier after a company suffix is part of the name, not a person; a romanised name still goes.
  assert.deepEqual(splitFilerName("Taro Yamada, President of ABC Co., Ltd. Japan"), { org: "ABC Co., Ltd. Japan", people: ["Taro Yamada"] });
  assert.equal(publicFilerName("KYOCERA CORPORATION Hideo Kasoumoto, Director and President"), "KYOCERA CORPORATION");
  // Organisations stay as they are, including ones whose name only looks like a title or a profession.
  for (const name of ["THECOO Inc.", "弁護士ドットコム株式会社", "弁護士法人〇〇", "Akatsuki Capital Works 株式会社", "株式会社 陣屋コネクト", "ブラックロック・ジャパン 株式会社"]) assert.equal(publicFilerName(name), name);
  assert.equal(publicFilerName("Akatsuki Capital Works 株式会社代表取締役 乙塚 郁人"), "Akatsuki Capital Works 株式会社");
});

test("register name: a person whose standing proxy is a broker is no organisation", () => {
  assert.equal(publicRegisterName("山田太郎（常任代理人 〇〇証券株式会社）"), null);
  assert.equal(publicRegisterName("山田 太郎"), null);
  assert.equal(publicRegisterName("ステート ストリート バンク アンド トラスト カンパニー (常任代理人 株式会社みずほ銀行決済営業部)"), "ステート ストリート バンク アンド トラスト カンパニー");
  const c = publicCompanyPeople({ major_shareholders: [{ name: "山田太郎（常任代理人 〇〇証券株式会社）", ratio_pct: 9 }, { name: "日本マスタートラスト信託銀行株式会社(信託口)", ratio_pct: 8 }] });
  assert.deepEqual(c.major_shareholders.map((s) => s.name), ["individual holder", "日本マスタートラスト信託銀行株式会社(信託口)"]);
});

test("holders and agents found in the local data: the person goes from every field", () => {
  const rec = { doc_id: "S100X9ID", issuer_ticker: "1", issuer_name_ja: "株式会社X", filer_edinet_code: "E38925", filer_name_ja: "PT ASIA PLASTIK VIKTORI",
    holders: [{ name: "PT ASIA PLASTIK VIKTORICEO FIKTIF", holder_type: "corporation" }] };
  for (const flags of [ORG, { excluded: 0, known_org: 0 }]) assert.doesNotMatch(JSON.stringify(publicHolding(rec, flags)), /FIKTIF/);
  const fe = { doc_id: "S100SXX2", issuer_ticker: "1", issuer_name_ja: "株式会社X", filer_edinet_code: "E06332", filer_name_ja: "弁護士 架 空 弘 典", filer_name_en: "First Eagle Investment Management, LLC",
    holders: [{ name: "ファースト・イーグル・インベストメント・マネジメント・エルエルシー", holder_type: "corporation" }] };
  const h = publicHolding(fe, ORG);
  assert.equal(h.filer_name_ja, null);
  assert.equal(h.filer_name_en, "First Eagle Investment Management, LLC");
  const e = publicEvent({ ...fe, type: "new_stake", event_id: "S100SXX2:new_stake", summary_en: "弁護士 架 空 弘 典 disclosed a 5.10% stake in X (1)." }, ORG);
  assert.equal(e.summary_en, "First Eagle Investment Management, LLC disclosed a 5.10% stake in X (1).");
  for (const x of [h, e]) assert.doesNotMatch(JSON.stringify(x), /架\s*空/);
});

test("foreign legal forms in katakana are organisations; katakana personal names are not", () => {
  for (const n of ["クナウフ・インタナショナル・ゲーエムベーハー", "アムンディ・オーストリア・ゲーエムベーハ―(Amundi Austria GmbH)", "クレディ・スイス・エイ・ジー", "ソシエテ・ジェネラル", "エネル・エスピーエー", "カッシーナエスピーエイ(Cassina S.p.A)",
    "ピージージーエム フェルモーホンビヘーア ビーブイ", "フリトレー・グローバル・インベストメンツ・ビー・ヴィ", "ノムラ・ヨーロッパ・ファイナンス・エヌ・ブイ", "アライアンス・バーンスタイン・ルクセンブルグ・エス・エイ・アール・エル",
    "ブラックロック・フランス・エスエーエス", "アムンディ・アイルランド・リミティッド", "アタナセ・インダストリアル・パートナー・エービー", "〇〇・アクチェンゲゼルシャフト"])
    assert.equal(looksLikeOrganization(n), true, n);
  for (const n of ["ジョン・スミス", "カソウ・タロウ・ジロウ", "エイミー・ジョーンズ", "山田 太郎"]) assert.equal(looksLikeOrganization(n), false, n);
});
