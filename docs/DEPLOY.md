# JapanStake デプロイと運用

対象: データパイプラインの無人運転。配信は Cloudflare Workers + D1、取込は GitHub Actions。
調査日: 2026-10-31(料金と上限)、数値の更新: 2026-11-05(リリース準備監査)。料金と上限は公式ページで確認した値で、文末の[出典](#出典)に番号で対応します。
公開の順番と本人作業の一覧は `docs/LAUNCH_CHECKLIST_JA.md` にあります。

**Workers Paid(月 $5)が必須です。** 手元の D1 相当 DB は約 715MB で、無料の上限 500MB/DB を超えるため、無料プランでは初回投入ができません。

**検証状況の表記**
- 【実測】2026-11-05 の手元ストア(5年遡及と議決権の収集後)で計測。古い節は日付を明記。
- 【試験済】pytest、actionlint、またはローカルの SQLite / `wrangler --local` で確認。
- 【未検証】アカウントが無いため実機未確認。**初回に本人が確認してください。** Cloudflare と GitHub への実接続は一度も行っていません。

## 1. 構成

```
EDINET / SEC / OpenFIGI
        │ (1 req/s、robots.txt 遵守)
        ▼
GitHub Actions  ingest.yml(同時に1本だけ。書き手は常に1つ)
  1 復元: Actions cache → R2 → どちらも無ければ停止(空のストアでは動かさない)
  2 取込: 情報源ごとに失敗を隔離。時間切れは次回に続きから
  3 検査: store-check(破損、または復元時から大幅に行数減なら以降を中止)
  4 差分: 行ハッシュと比較し、変更行だけをチャンク化
  5 送信: migrations 適用 → チャンクごとに D1 へ(成功分だけハッシュを記録)
  6 保存: ストア+ハッシュ状態を1つの tar.gz に → cache、夜間は R2 にも
  7 判定: 失敗した手順があればジョブを失敗に → 本人へメール
        │
        ▼
Cloudflare D1 ◀── Workers(API・サイト)   watchdog.yml が /v1/status を6時間ごとに確認
```

| 実行 | 時刻(JST) | 内容 | D1 へ送る表 |
|---|---|---|---|
| fast | 平日 08:03–20:53、10分ごと | 当日と前日の書類一覧、5%報告、有報 | filings ほか5表 |
| nightly | 日〜金 21:17 | 直近7日の取り直し、下記の全ジョブ、R2 バックアップ | 全表 |
| weekly | 土 21:17 | nightly + コードリスト、FIGI、口座情報の暗号化バックアップ | 全表 |

**10分ジョブを止めない方法**: 長いジョブは 10分ジョブの無い夜間に実行します。全実行が同じ concurrency グループなので、並行書込みで行が失われることはありません。

**夜間の時間配分**: ジョブ上限は 355 分です。長い収集(buybacks、governance、filing-details、voting、tender-offers、ratings、halfyear、backfill、reconcile、FIGI)は1つの持ち時間を共有します。開始時に締切 `COLLECT_DEADLINE` を決め(nightly は開始から180分、weekly は165分)、各手順は「自分の上限」と「締切までの残り」の短い方で打ち切られ、次回に続きから再開します。残り5分未満なら起動せず警告だけ出します(実行の Summary に「Skipped for lack of time」)。締切の後に残す時間(nightly 175分、weekly 190分)は、指標、更新キュー、検査、差分、D1 送信(60分)、R2 公開、スナップショット、cache 保存、R2 バックアップ、口座バックアップの各 `timeout-minutes` の合計なので、保存と送信は必ず走ります。短い固定手順(codelist、投資家グループ)は締切より前、`indicators` は締切の後に移しました。後段の `timeout-minutes` を変えたら `ingest.yml` の `RESERVE` も直してください。

**アカウント情報の扱い**: `users`、`api_keys`、`sessions`、`webhooks` などは Worker だけが書きます。エクスポート対象外なので、差分送信で変更も削除もされません(テストで確認)。

## 2. 本人の作業手順(順番どおりに)

作業は `jp_disclosure_api/` で行います。

**前提**: 実行中の収集(2026-11-05 時点では公開買付の取り直し)の完了を待ち、ストアを固定します。残りの量は `python3 -m japanfilings.cli report` では分からないので、`ps aux | grep japanfilings` が空になることで確認してください。

### 2.0 個人情報を出さないための設定(最初に)

- **workers.dev のサブドメイン**: Cloudflare が最初に提案する名前はメールアドレス由来のことがあります。`japanstake` など中立な名前にしてから Worker を作成します。
- **GitHub**: ユーザー名は本名やメールと無関係なものにします。Settings → Emails で「Keep my email addresses private」と「Block command line pushes that expose my email」を有効にします。
- **コミットの作成者**: このリポジトリだけ、作成者を GitHub の noreply アドレスにします(全体設定は個人の Gmail のため、そのままだと公開履歴に残ります)。
  ```
  git config user.name "japanfilings"
  git config user.email "<ID>+<ユーザー名>@users.noreply.github.com"   # GitHub の Settings → Emails に表示される値
  ```
  最初の push の前に `git log --format='%an %ae'` で確認します。
- **送信元メール `MAIL_FROM`**: 独自ドメインのアドレス(例 `noreply@<ドメイン>`)にします。個人の Gmail は使いません。`CONTACT_EMAIL`(通知先)はサイトに表示されません。
- **ドメインの WHOIS**: Cloudflare Registrar は登録者情報を既定で非公開にします。他社で取る場合は WHOIS 代理公開を有効にします。
- **SEC の User-Agent**: 連絡先メールは GitHub Secret `SEC_USER_AGENT` にだけ入れます(ログでは伏せ字になります)。

### 2.1 Cloudflare

Cloudflare のアカウントを作成し、支払い方法を登録して **Workers Paid($5/月)に加入**します(ダッシュボードの Workers & Pages → Plans)。D1 が約 715MB で無料の 500MB/DB を超えるためです。R2 も支払い方法の登録が前提です。

```sh
cd worker && npm ci
npx wrangler login
npx wrangler d1 create japanfilings        # 表示された ID を wrangler.toml の database_id に貼る
npx wrangler d1 migrations apply japanfilings --remote

npx wrangler r2 bucket create japanfilings-backups
npx wrangler r2 bucket lifecycle add japanfilings-backups daily-7d daily/ --expire-days 7
npx wrangler r2 bucket lifecycle add japanfilings-backups weekly-56d weekly/ --expire-days 56
npx wrangler r2 bucket lifecycle add japanfilings-backups accounts-365d accounts/ --expire-days 365

npx wrangler secret put RESEND_API_KEY
npx wrangler secret put MAIL_FROM
npx wrangler secret put CONTACT_EMAIL      # 問い合わせフォームの通知先(サイトには出ない)
npx wrangler secret put EDINET_API_KEY     # 5分ごとの即時取得(ondemand.ts)。無いと /v1/status に未設定と記録され取得しない
cd ..
```

`wrangler deploy` は初回データの投入とスナップショットの後(2.2 の最後)に行います。

| Worker の値 | 設定 | 用途 |
|---|---|---|
| `RESEND_API_KEY`、`MAIL_FROM`、`CONTACT_EMAIL`、`EDINET_API_KEY` | 必須(`wrangler secret put`) | メール、連絡先、即時取得 |
| `PUBLIC_ORIGIN` | 独自ドメインにしたら設定(例 `https://<ドメイン>`) | OAuth の発行者 URL。未設定なら要求の URL |
| `LEGAL_PAGE_ENABLED`、`LEGAL_PROVIDER_NAME`、`LEGAL_START_OF_SERVICE`、`LEGAL_CANCELLATION`、`LEGAL_REFUNDS` | **弁護士確認までは設定しない** | 全て埋まり `LEGAL_PAGE_ENABLED=true` のときだけ `/legal` を表示。それまで404 |
| `PADDLE_*`(`PADDLE_PRICE_STARTER`、`PADDLE_PRICE_PRO` を含む)、`OAUTH_ENABLED` | 設定しない | 課金は弁護士確認まで閉じたまま。全プラン有料(Starter $9 / Pro $39。機能は同じで上限だけが違う)なので、課金を開くまで API は誰も使えない |
| `DEV_SHOW_LINKS` | **本番に設定しない** | 開発用 |

ダッシュボードで入れた平文の変数は次の `wrangler deploy` で消えます。値は `wrangler secret put` で入れてください。

### 2.2 初回投入(Mac から、1回で)

**推奨: 本人の Mac から1回で全件。** CI の差分送信(`D1_DAILY_WRITE_CAP=1000000`)に任せると約5日かかり、その間サイトのデータが欠けます。無料プランの 8万/日では約55日で、容量超過のためそもそも不可です。

| 項目【実測 2026-11-05】 | 値 |
|---|---|
| 送信対象 | 27表、1,064,417行 |
| 推定書込(行 ×(1 + 索引数)、多めの見積り) | 約436万行(うち holder_trades 224万)。Paid の月込み 5,000万行の約9% |
| チャンク | 約175本(各 25,000 書込相当、5MB 以下、1文 100KB 以下) |
| 所要 | 未計測(1チャンク数十秒なら1〜3時間)。途中で止めても再開可 |

wrangler 4.156(`worker/package.json` の固定版)には `d1 import` がありません。`d1 execute --remote --file` が D1 の取込 API を使います。既存の `delta --init` + `push` は「100KB 以下の文、5MB 以下のチャンク、成功分だけ記録、途中再開可」なので、これを一括投入として使います。

```sh
# ストアを固定してから(収集プロセスが無いこと)
python3 -m japanfilings.export_d1 delta --init --out-dir data/d1_delta --state data/d1_state.db --daily-cap 5000000
python3 -m japanfilings.export_d1 push --dir data/d1_delta --state data/d1_state.db
#   止まったら同じ push を再実行(送信済みチャンクは送らない)
python3 -m japanfilings.export_d1 delta --out-dir data/d1_delta --state data/d1_state.db --daily-cap 5000000
#   → chunks=0 と出れば全件送信済み
(cd worker && npx wrangler d1 execute japanfilings --remote --command "SELECT COUNT(*) FROM holder_trades")   # 手元と件数を照合
```

最初のチャンクが通ったら、ダッシュボードの書込み行数と `push` の推定値を見比べてください【未検証: 推定は多めのはず】。タイムアウトする場合は `delta` に `--chunk-writes 5000` を付けます。

予備(通常は使わない): `data/d1_load.sql`(747MB、最長 29KB/文)を `split -l 100000` で約11本に分け、順に `npx wrangler d1 execute japanfilings --remote --file <片>` で流し、直後に**同じストアのまま** `python3 -m japanfilings.export_d1 baseline --state data/d1_state.db` で送信済みとして記録します。間にストアが変わると差分が狂います。

次に、CI の出発点となるスナップショットを R2 に置き、Worker を公開します。

```sh
python3 -m japanfilings.cli snapshot data/snapshot.tar.gz      # 推定 約200MB(DB 圧縮 151MB【実測】+ ハッシュ状態)
(cd worker && npx wrangler r2 object put japanfilings-backups/latest.tar.gz --remote --file ../data/snapshot.tar.gz)
(cd worker && npx wrangler deploy)   # cron */5 もここで有効。スクリーナーは次の 21:00 JST に構築され、それまで空
```

`wrangler r2 object put` の上限は 315MB[C6] で、年 0.2〜0.25GB の増加では2〜3年で届きます。その前に分割か S3 互換アップロードへ切り替えてください。

**これ以降の正本は CI 側のストアです。** Mac の `data/japanfilings.db` への追記は D1 に届きません。`calls` の手順は[6章](#6-運用手順書)にあります。

### 2.3 Cloudflare API トークン

ダッシュボードの My Profile → API Tokens で Custom Token を作成します。権限は Account の **D1: Edit** と **Workers R2 Storage: Edit** のみ。他の権限が要求された場合はエラー内容に従って追加してください【未検証】。

### 2.4 GitHub

push した時点で schedule が動き出すので、**リポジトリを空で作り、Secret と変数を先に登録してから push** します。

```sh
git init -b main
git add -A
git status                    # data/ は1つも無いこと。.env、key_*.txt、*.db、japanfilings/private/、worker/.dev.vars、worker/.wrangler/、worker/undefined/ が無いこと
git check-ignore data/key_pro.txt data/japanfilings.db .env worker/.dev.vars japanfilings/private/lexicon.json   # 5行とも表示されること
git commit -m "Initial commit"
gh repo create japanfilings --public          # 空で作る(--source/--push は付けない)
R=<GitHubのユーザー名>/japanfilings

gh secret set EDINET_API_KEY -R "$R"
gh secret set CLOUDFLARE_API_TOKEN -R "$R"
gh secret set CLOUDFLARE_ACCOUNT_ID -R "$R"
gh secret set SEC_USER_AGENT -R "$R"            # "氏名 メールアドレス"
gh secret set BACKUP_PASSPHRASE -R "$R"         # 先にパスワード管理に保存した値を貼る(下記)
gh secret set LEXICON_JSON -R "$R" < japanfilings/private/lexicon.json   # 非公開の語彙。無いと ingest が止まる
gh variable set STATUS_URL -R "$R" --body "https://<独自ドメイン>/v1/status"
gh variable set D1_DAILY_WRITE_CAP -R "$R" --body 1000000                 # Workers Paid 前提(必須)

git remote add origin "https://github.com/$R.git"
git push -u origin main
```

`BACKUP_PASSPHRASE` は GitHub から読み戻せません。先に `openssl rand -base64 32` の結果をパスワード管理に保存し、その値を `gh secret set` に貼る方法が確実です。

GitHub の Settings → Notifications → Actions で「失敗のみメール」を有効にします。

### 2.5 独自ドメイン

1. ドメインを取得し、Cloudflare に追加してレジストラでネームサーバーを Cloudflare に変更(反映に数時間〜)。
2. Workers & Pages → `japanfilings-api` → Settings → Domains & Routes → Add → Custom domain でドメインを追加。または `wrangler.toml` に次を足して `npx wrangler deploy`:
   ```toml
   routes = [{ pattern = "<ドメイン>", custom_domain = true }]
   ```
3. `npx wrangler secret put PUBLIC_ORIGIN`(`https://<ドメイン>`。wrangler.toml の [vars] には書かない: 書くと手元のテストもそのドメインを使う)。旧 workers.dev への要求は PUBLIC_ORIGIN に 308 で転送される。GitHub の変数 `STATUS_URL` も独自ドメインの `/v1/status` にする。
4. Resend で `MAIL_FROM` のドメインを認証(DNS に SPF/DKIM)し、ログインメールが届くことを確認。

【未検証】アカウントが無いため、画面の名称は変わっている可能性があります。

### 2.6 動作確認

```sh
gh workflow run ingest.yml -f mode=fast && gh run watch
gh workflow run ingest.yml -f mode=weekly     # 夜間ジョブ、R2、口座バックアップを確認
gh workflow run watchdog.yml
curl -s "$STATUS_URL"
# R2 の中身はダッシュボードで確認(wrangler 4.156 に object list はありません)
```

fast 1回の所要時間を記録してください(10分未満であること。ストアが約1.2GBに増え、従来の約3分の見積りは未再計測)。初週は毎日 Actions と D1 のダッシュボード(書込行数、容量)を確認します。

失敗メールが届くかは、`STATUS_URL` を一時的に誤った値にして watchdog を動かすと確認できます。

## 3. 公開か非公開か

**推奨: 公開リポジトリ。** 標準ランナーが無料だからです[G1]。

| | 公開 | 非公開(Free $12相当込み) |
|---|---|---|
| Actions 費用(10分間隔) | **$0** | **月 約 $29–46**(下の推定) |
| 非公開で30分間隔に落とす場合 | — | 月 約 $9–25 |
| 60日無活動で schedule 停止[G4] | あり(watchdog が45日で空コミット) | 公式の記載は公開リポジトリのみ |
| コードの露出 | あり | なし |

分数の内訳【推定、未検証】: fast 1,695回/月 × 約3分 = 約5,100分。nightly 30回 × 60–190分(上限 355分) = 1,800–5,700分。合計 約6,900–9,600分 × $0.006[G1] = $41–58、含まれる $12 を引いて $29–46。Free の含有分を超えると、予算設定によっては実行が止まる可能性があり、その場合は watchdog が検知します。

公開時の確認結果:
- **秘密情報**: 【試験済】`.env`、`data/key_*.txt`、DB は `.gitignore` 済み。リポジトリ対象ファイルに本人のメールアドレスは無し(grep)。秘密は `secrets` から必要な手順の env にのみ渡し、シェルに直接埋め込みません(テストで確認)。
- **ログ**: 公開されます。出力は銘柄コード、書類ID、件数、公開書類由来の短いエラーだけです。wrangler の標準出力は捨てます。利用者データの処理は週次の口座バックアップのみで、暗号化前の内容は出力せず、cache にも入れません。
- **フォーク経由の攻撃**: `pull_request` トリガーが無いので、外部 PR で secrets は使われません。
- **残るリスク**: コードは複製されうる(データは含まれません)。LICENSE を置かなければ権利は留保されます。コード自体を競争力と考えるなら非公開を選び、上の費用を払ってください。

## 4. 月額費用

| 項目 | 無料段階 | 有料段階(**必須**) | 根拠 |
|---|---|---|---|
| Workers | $0(10万リクエスト/日、CPU 10ms/回) | $5(1,000万リクエスト、3,000万CPUms 込み) | [C3] |
| D1 | $0(書込10万行/日、読取500万行/日、1DB 500MB) | $0 追加(書込5,000万行/月、読取250億行/月、5GB 込み、1DB 10GB) | [C1][C2] |
| R2 バックアップ | $0(10GB-月まで。固定名12個×最大300MB+口座8個×50MB=最大4GB。300MB超は送らず失敗にする) | $0 | [C4] |
| GitHub Actions(公開) | $0 | $0 | [G1] |
| Resend、ドメイン、決済 | 未調査 | 未調査 | 【未検証】 |
| **合計(確認済み分)** | 利用不可(容量超過) | **$5** | |

**有料プランは必須です。**
- 容量: 手元の D1 相当 DB は約 715MB【実測 2026-11-05】で、無料の 500MB/DB を超えます。
- 初回投入: 約436万行の書込で、無料の 8万/日では約55日。有料なら月枠の約9%を1回で使います。
- Free の CPU 10ms/回と 50クエリ/回[C1][C3]に API が収まるかも【未検証】です。

### D1 の実測と上限

| 項目 | 公式の上限 | 本プロジェクト【実測 2026-11-05】 |
|---|---|---|
| DB サイズ | Free 500MB、Paid 10GB[C1] | 約715MB。大きい順に large_holdings 182MB、holder_trades 97MB(+索引 44MB)、annual_reports 86MB、ownership_events 82MB |
| SQL 文の長さ | 100KB[C1] | 最大 29,259 バイト。超える行は自動で分割(試験済) |
| 取込ファイル | 5GB[C1][C5] | 約175チャンク(各5MB以下)。一括 SQL は 747MB |
| クエリ時間 | 30秒[C1] | 1チャンク最大 25,000 行相当【未検証: 取込に適用されるか不明】 |
| 全件の行数 | — | 27表、1,064,417 行 |
| 推定書込 | — | 約436万行(holder_trades 224万) |
| スナップショット | wrangler 上限 315MB[C6] | 推定 約200MB(DB 圧縮 151MB + ハッシュ状態) |

**年間増加【推定】**: 約25万行/年、書込 約100万行/年、容量 約0.2〜0.25GB/年(有報と詳細が毎年約4,500社分増える前提)。Paid の込み 5GB には十数年、上限 10GB には更に先です。

`company_facts` と `sector_benchmarks` は送信しません。Worker が毎日 21:00 JST に D1 内で作ります(`screener.ts`)。初回投入後、次の 21:00 までスクリーナーは空で、初回の構築に約 3.8 万行の書込が別途かかります【推定】。夜間の送信は 21:17 以降なので、この2表は1日遅れです。

**書込行数の係数**: Cloudflare は「表の行 + インデックスごとに1行」と数えます[C2]。主キーの自動インデックスも数える前提で、1 + D1 のインデックス数(migrations から算出)を使います。実際より多めのはずです。`wrangler --local` は `rows_written` を返さず、ローカルでは確認できませんでした【未検証】。

**差分送信後の見込み【推定、2026-10 時点の計算で未更新】**:
- 通常日 約 4,100 行(2026年10月の日平均から: 書類約 230 件 × 5、取引 594 × 4 ほか)。
- 年間最大日 約 66,000 行(2026-06-25 の書類 1,878 件、有報 416 件、持合い行の入替、全社指標)。
- `indicators` を翌日付で再計算しても、変更は 3,825 社中 1 社でした【実測、新規書類なしの条件】。
- 日次上限 `D1_DAILY_WRITE_CAP`(本番は 1,000,000。未設定なら無料向けの 8万)を超えた行は翌 UTC 日に回ります。Worker 自身の書込用に2万を残しています。上限のリセットは 09:00 JST(00:00 UTC)です[C2]。

## 5. 情報源ごとの更新スケジュール

間隔は EDINET の順次アクセス 1 req/s(`edinet.py`。公式の数値上限は確認できず、自主基準)を守ります。EDINET を使う手順は直列です。

| データ | 情報源 | コマンド | 頻度 | 遅延の目安 |
|---|---|---|---|---|
| 書類一覧、5%報告、取引、イベント | EDINET API | `range` | 平日10分ごと、夜間に7日分再取得 | 約10分 + Actions の遅延[G4] |
| 有価証券報告書、持合い | EDINET API | `annual` | 同上(持合いの D1 反映は夜間) | 10分〜翌朝 |
| 自己株券買付状況報告書 | EDINET API | `buybacks` | 毎晩(未読分のみ) | 翌朝 |
| 内部統制、監査人異動 | EDINET API | `governance` | 毎晩(未読分のみ) | 翌朝 |
| 社債書類の格付 | EDINET API | `ratings` | 毎晩(未読分のみ) | 翌朝 |
| 半期報告 | EDINET API | `halfyear` | 毎晩 | 翌朝 |
| 5%報告の過去分 | EDINET API | `python -m japanfilings.backfill` | 変数 `BACKFILL_ENABLED=1` のときのみ毎晩50分 | — |
| 財務指標の percentile、政策不確実性の記述量、ownership activity の件数 | 自社計算 | `indicators` | 毎晩(`range` でも再計算) | 翌朝 |
| コードリスト、FIGI | EDINET、OpenFIGI | `codelist`、`figi` | 週1回 | 最大1週間 |
| 米ファンド保有 | SEC N-PORT | `gh workflow run ingest.yml -f nport_quarter=2026q4` | 四半期(手動) | 四半期末の約2か月後 |

## 6. 運用手順書

### 失敗メールが来たら

Actions の該当実行を開くと、最後の手順に `Failed steps:` が出ます。

| 失敗手順 | 主な原因 | 対応 |
|---|---|---|
| `fast_filings`, `catch_up`, `buybacks` など | EDINET の保守、キー期限切れ | 保守なら放置(次回続きから、7日以内なら夜間で回復)。401/403 ならキーを更新 |
| (失敗ではない)Summary に `Skipped for lack of time` | 夜間の持ち時間切れ | 次回続きから。毎晩同じ手順が飛ぶなら、前段の上限か締切(`ingest.yml` の `RESERVE`)を見直す |
| `nport` | 持ち時間切れ(124)、SEC の障害 | `-f mode=fast -f nport_quarter=...` で再実行(fast は長い収集が無く持ち時間を全部使える) |
| `restore` | cache も R2 も無い | 下の「復元」 |
| `check` | ストア破損、行数減 | **送信も保存も止まっています。** 下の「復元」 |
| `export`(REFUSED) | 全件または20%超の削除 | 下の「大量削除」 |
| `push` | D1 上限、トークン、一時障害、60分の打ち切り | 3回再試行済み。送信済みは記録済みで、次回残りを送信。続くならトークンと上限を確認 |
| `backup` | R2 のトークン、容量 | cache は健在。その日のうちに修正 |
| `accounts` | パスフレーズ未設定、トークン | 修正して `-f mode=weekly` |
| watchdog `status` | データが5日超古い、API 停止 | ingest の履歴を確認。**GW と年末年始は誤報の可能性があり、その場合は無視** |
| watchdog `ingest` | 30時間成功なし、schedule 停止 | Actions の画面で有効化。無料枠切れも確認 |

10分ジョブが壊れている間はメールも10分ごとに届きます。修正までの間は `gh workflow disable ingest.yml` で止められます(再開は `enable`)。再有効化すると通知先はその操作をした人になります[G5]。

### 復元(上から順に試す)

ストアとハッシュ状態は常に同じ tar.gz に入っています。古い世代へ戻しても両者は矛盾せず、不足分は再取込で埋まります。

**A. cache が消えただけ**: 操作不要。自動で R2 の `latest` から復元します(7日未使用で cache は消えます[G3])。古くても1日分で、次の実行で取り戻します。

**B. `latest` が壊れている**: 別の世代を置き直します。
```sh
cd worker
# R2 は固定名で上書き(daily/1..7 は曜日、weekly/0..3 は週番号 mod 4、accounts/0..7)。日付はダッシュボードの R2 画面で確認
npx wrangler r2 object get japanfilings-backups/daily/6.tar.gz --remote --file ../data/good.tar.gz   # 1=月…7=日、weekly/0..3
npx wrangler r2 object put japanfilings-backups/latest.tar.gz --remote --file ../data/good.tar.gz
gh cache list --key store- --json id --jq '.[].id' | xargs -n1 gh cache delete   # 壊れた cache を消す
gh workflow run ingest.yml -f mode=nightly
```
7日以上戻す場合は、Mac で `range <開始日> <今日>` と `annual` を走らせてから同様に戻します。

**C. R2 も全滅、D1 は健在**【未検証、一度リハーサルを推奨】: 別ディレクトリに D1 から復元します。D1 には内部表(`voting_read` 等)と、レコード内の `trades`、`cross_holdings`、`text_indicators` がありません。

```sh
mkdir -p /tmp/recover && export JAPANFILINGS_DB=/tmp/recover/japanfilings.db
python3 -c "from japanfilings import store, export_d1 as e; e.ensure_schema(store.connect())"
TABLES=$(python3 -c "from japanfilings import export_d1 as e; print(' '.join('--table '+t for t in e.TABLES))")
(cd worker && npx wrangler d1 export japanfilings --remote --no-schema $TABLES --output /tmp/recover/d.sql)
sqlite3 /tmp/recover/japanfilings.db < /tmp/recover/d.sql
python3 -m japanfilings.cli recheck           # 取引を EDINET から再構築。1 req/s
python3 -m japanfilings.cli annual-reparse    # 持合いを再構築。約4,500件、1時間超
python3 -m japanfilings.cli text-reindex
```
**順序厳守。** `recheck` と `annual-reparse` の前に `range` などの再構築を走らせると、`holder_trades` と `cross_holdings` が 0 件になります【試験済: D1 相当の DB で再現】。この場合 `store-check` と差分の安全装置が送信を止めます。
その後、D1 と同じ内容なので送信不要として記録し、R2 に置きます。
```sh
python3 -m japanfilings.export_d1 baseline --state /tmp/recover/d1_state.db
python3 -m japanfilings.cli snapshot /tmp/recover/s.tar.gz
(cd worker && npx wrangler r2 object put japanfilings-backups/latest.tar.gz --remote --file /tmp/recover/s.tar.gz)
```
`--table` の複数指定は `wrangler d1 export --help` で確認済みですが、実行は未検証です。実行中は D1 への他の要求が止まります[C5]。

**D. 全滅**: 公的データは EDINET から再取得できます(`codelist` → `range <開始> <終了>` → `annual` → 各ジョブ → `delta --init`)。1 req/s で丸1日以上かかるので Mac で実行します。

**D1 だけが壊れた**: まず Time Travel で戻します(Free 7日、Paid 30日[C1])。
```sh
npx wrangler d1 time-travel restore japanfilings --timestamp <UNIX時刻>
```
新しい DB を作る場合は `migrations apply` の後、`rm data/d1_state.db` と `delta --init`(書込 約436万行、2.2 と同じく手元から `--daily-cap 5000000`)で再投入し、口座情報は下で戻します。

**口座情報の復元**:
```sh
npx wrangler r2 object get japanfilings-backups/accounts/<日付>.sql.enc --remote --file a.enc
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in a.enc -out a.sql
npx wrangler d1 execute japanfilings --remote --file a.sql
rm a.enc a.sql
```
パスフレーズを失うと、このバックアップは読めません。

**R2 は D1 と同じアカウントです。** アカウント喪失に備え、月1回 `latest.tar.gz` と最新の `accounts/*.enc` を Mac か別の場所へコピーしてください。

### 大量削除

`export` の REFUSED は、ある表が空になる、または100行超かつ20%超が消える場合に出ます。まず原因を調べます。
- 有報の大量提出期の持合い入替や、書類の取下げなら正当なことがあります。
- 意図したものと確認できたら `gh workflow run ingest.yml -f mode=nightly -f allow_mass_delete=true`。
- 不明なら送らず、復元を検討します。

### 鍵の更新

| 鍵 | 手順 |
|---|---|
| `CLOUDFLARE_API_TOKEN` | 新トークン作成 → `gh secret set` → `gh workflow run ingest.yml -f mode=fast` で確認 → 旧トークン削除 |
| `EDINET_API_KEY` | EDINET で再発行 → `gh secret set`、`npx wrangler secret put EDINET_API_KEY`、`.env` を更新 |
| `LEXICON_JSON` | `japanfilings/private/lexicon.json` を直したら `gh secret set LEXICON_JSON < japanfilings/private/lexicon.json` |
| `BACKUP_PASSPHRASE` | 新しい値を保存 → `gh secret set`。過去のバックアップは旧値でしか読めないので、365日は旧値も保管 |
| `RESEND_API_KEY` | `npx wrangler secret put RESEND_API_KEY` |
| 秘密が漏れた場合 | 上記に加え、Actions のログを確認し該当実行を削除。git に入った場合は履歴を書き換えても漏洩扱いで全て更新 |

### 既知の制約

- 5年分の遡及(5%報告 73,149件)は手元で完了し、上の数値に含まれます。CI の `backfill` は既定で無効のままです(`BACKFILL_ENABLED=1` で毎晩最大50分、夜間の持ち時間の範囲内)。
- `/v1/status` は 5%報告の最新日しか返しません(worker/src は今回の変更対象外)。他の情報源の遅れは ingest の失敗メールで検知します。
- 無活動停止の回避に使う空コミットが「活動」と数えられるかは【未検証】です。watchdog 自身も止まるため、カレンダーで2か月に1度 Actions の画面を確認してください。
- schedule は遅延し、混雑時は実行が落ちることがあります[G4]。そのため毎時ちょうどを避け、夜間に7日分を取り直します。
- README の Known limits にある「週次全件エクスポート」「`releases` は週次」の記述、Secret の一覧(`LEXICON_JSON` が無い)、API トークン権限(「D1 + Workers deploy」。正しくは本書 2.3 の D1 と R2)は古いままです(今回の編集範囲外)。

## 出典

確認日はすべて 2026-10-31。

- [C1] D1 Limits: https://developers.cloudflare.com/d1/platform/limits/
- [C2] D1 Pricing: https://developers.cloudflare.com/d1/platform/pricing/
- [C3] Workers Pricing: https://developers.cloudflare.com/workers/platform/pricing/
- [C4] R2 Pricing: https://developers.cloudflare.com/r2/pricing/
- [C5] D1 Import and export: https://developers.cloudflare.com/d1/best-practices/import-export-data/
- [C6] R2 Upload objects: https://developers.cloudflare.com/r2/objects/upload-objects/
- [G1] GitHub Actions billing: https://docs.github.com/en/billing/concepts/product-billing/github-actions
- [G2] Actions limits: https://docs.github.com/en/actions/reference/limits
- [G3] Dependency caching(7日、10GB): https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching
- [G4] schedule イベント(5分間隔、遅延、60日): https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- [G5] ワークフロー通知: https://docs.github.com/en/actions/concepts/workflows-and-actions/notifications-for-workflow-runs


## 非公開の語彙ファイル
`japanfilings/private/lexicon.json` はリポジトリに含めません。GitHub Actions の Secret `LEXICON_JSON` に同じ内容を登録してください(2.4)。無いと ingest は明確なエラーで止まります。
