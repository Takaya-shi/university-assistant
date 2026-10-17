# university-sync

Google Classroom・学校メール（Gmail）から課題・締切・資料を毎日取り込み、Obsidian Vault の課題ノートとタスク（[Tasks](https://github.com/obsidian-tasks-group/obsidian-tasks) プラグイン形式）に同期する、個人用の CLI です。Claude Code 用の Skill と組み合わせて、資料の解説や回答の整理にも使えます。

自分の Mac で動かし、自分の Google Cloud プロジェクト・自分の Claude を使う前提です（ホスティングされたサービスではありません）。

## できること

- **Classroom**：授業・課題・自分の提出状態・資料・お知らせを読み取り、課題ごとのノートとタスク行（`📅` 締切付き）を作る。期限変更・提出状態の変化も反映し、提出が確認された課題は自動で完了にする。
- **資料一覧**：授業ごとに、資料の投稿・お知らせの添付・課題の添付をまとめた `資料一覧.md` を作る。Drive の資料を個人の Drive へコピーすることもできる（ダウンロード禁止の資料は写さない）。
- **締切カレンダー**：締切のある課題を、個人アカウントの指定した Google カレンダーに予定として登録する（締切の 30 分前〜締切、通知は前日と 30 分前。時刻の無い締切は終日）。期限変更は予定を動かし、提出済みは件名に【提出済】を付けて通知を外す。本人が予定を消したり件名・時刻を変えたりしたら、その予定には以後触れない。
- **学校メール**：指定したドメインからのメールを Claude（Claude Code CLI）で読み、課題・締切の候補を抽出する。候補は要確認一覧に並べるだけで、自動では登録しない。Classroom の通知メールは既存の課題に紐付けるだけ。
- **安全側の作り**：書き込むのは許可したフォルダの管理ブロックだけ。本人が書いた回答欄や、本人が編集した部分は上書きしない。取得に失敗しても課題を消さない。dry-run あり。
- **毎日の自動実行**：macOS の launchd で毎朝起動し、Mac を止めていた期間も次回に取り戻す。

## 必要なもの

- macOS、Python 3.11 以上、Obsidian（Tasks プラグイン）
- 自分の Google Cloud プロジェクト（OAuth クライアント「デスクトップアプリ」。Classroom API・Gmail API・Drive API を有効化。締切カレンダーを使うなら Google Calendar API も）
- メールの解析に Claude を使う場合は [Claude Code](https://claude.com/claude-code)

## 使い方

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[google]'
mkdir -p ~/.config/university-sync
cp config.example.toml ~/.config/university-sync/config.toml   # 空欄は推測で埋めない

.venv/bin/university-sync doctor              # 設定・書込先の検証、未設定項目の一覧
.venv/bin/university-sync auth school         # 学校アカウントの認可（ブラウザが開く）
.venv/bin/university-sync auth storage        # 保存先 Drive のアカウントの認可
.venv/bin/university-sync courses             # 授業一覧。同期する授業を config の [[courses]] に書く
.venv/bin/university-sync run --dry-run       # 変更計画だけ表示（Vault・状態・Drive を変えない）
.venv/bin/university-sync run                 # 同期
.venv/bin/university-sync status              # 最終成功・書込待ち・要確認
.venv/bin/university-sync review              # 要確認・メール候補（--accept / --dismiss / --resolve）
.venv/bin/university-sync schedule install --time 07:00   # 毎日の自動実行（launchd）
```

試すだけなら、Google に接続しないサンプルデータで動かせます。

```bash
.venv/bin/university-sync init-test-vault .test-vault
# config で vault.path を .test-vault、source.mode を "sample" にする
.venv/bin/python -m unittest discover -s tests
```

終了コード: 0 正常 / 1 一部失敗 / 2 失敗 / 3 設定・認可・安全条件で停止

## 権限について

| アカウント | スコープ | 用途 |
| --- | --- | --- |
| 学校 | `classroom.*.readonly` | 授業・課題・提出状態・資料・お知らせの読み取り |
| 学校 | `gmail.readonly` | 学校メールの読み取り（メールボックス全体を読める権限。対象の絞り込みはプログラム側） |
| 学校 | `drive.readonly` | Classroom 資料のコピー元（資料コピーを使うときだけ） |
| 保存先 | `drive.file` | このツールが作ったファイル・フォルダだけを扱う |
| 保存先 | `calendar.events.owned` | 自分が所有するカレンダーの予定の作成・更新・削除（締切カレンダーを使うときだけ。書くのは `deadline_calendar_id` の 1 つだけ） |

資格情報は macOS のキーチェーンに保存し、ログや Vault には出しません。メールの送信・既読化・削除、課題の提出はしません。

## 構成

| モジュール | 役割 |
| --- | --- |
| `collectors/` | 取得元（Classroom・Gmail・サンプル） |
| `normalize.py` `timeutil.py` | 共通スキーマへの変換（UTC→JST、内容の指紋） |
| `sync.py` | 照合・同期計画・適用・復旧・ロック |
| `resources.py` `materials.py` | 資料一覧、資料のコピー |
| `gcalendar.py` | 締切カレンダーへの登録 |
| `mailsync.py` `llm.py` `mail.py` | メールの取得・Claude による候補抽出と検証 |
| `vault.py` `render.py` | Vault への書き込み（パス保護・管理ブロック・タスク行・原子的書込・バックアップ） |
| `state.py` | 状態ストア（SQLite、Vault の外） |
| `schedule.py` | launchd による定期実行 |
| `skill/uec/SKILL.md` | Claude Code 用 Skill の雛形（`~/.claude/skills/<名前>/` に置き、パスを書き換えて使う） |
| `site/` | OAuth 同意画面用のプライバシーポリシーページ |

## 注意

- 個人が自分のために使うことを想定したツールです。他人のデータを扱う形で運用する場合は、Google の OAuth 審査や各サービスの規約を確認してください。
- 大学の学習システムの利用規約・授業ごとの生成 AI の利用ルールに従ってください。
