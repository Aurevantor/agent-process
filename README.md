# agent-process

Codex CLI (`codex exec`) に **1 回だけ** の bounded なプロンプトを渡して終了する、
依存ゼロの Python ラッパーです。ワンショット委譲におけるプロセス／成果物の境界を
担当します。高レベルの spawn/wait API や会話状態の管理は行いません。

- Python 3.11 以上、標準ライブラリのみ
- 既定は `read-only` + `--ephemeral`（副作用なしの使い捨て実行）
- プロンプトは stdin 経由で渡し、argv や診断ログには出さない
- 再帰委譲を事故防止としてブロック（`AGENT_PROCESS_NESTING=1`）

## 必要要件

- Python `>=3.11`
- Codex CLI（`codex` が `PATH` にあること。`--codex-bin` / `AGENT_PROCESS_CODEX_BIN` で変更可）

## インストール

このリポジトリのチェックアウトから、CLI とスキルを symlink で設置できます。

```sh
# 何が作られるかを確認
python3 .agents/skills/agent-process-install/scripts/install_local.py --dry-run

# 作成
python3 .agents/skills/agent-process-install/scripts/install_local.py

# 検証
python3 .agents/skills/agent-process-install/scripts/install_local.py --check
agent-process --version
```

`pip` で入れることもできます。

```sh
pip install .
```

## 使い方

```sh
# 位置引数としてプロンプトを渡す
./agent-process --model codex-spar -- "現在の変更をレビューし、具体的な指摘を列挙して"

# stdin からパイプする（`-` で明示的に stdin を選択）
printf '%s\n' "この設計の失敗モードを評価して" | ./agent-process --model luna-max -

# 書き込みを許可する（親が編集を明示的に許可した場合のみ）
./agent-process --write --model codex-spar -- "テストを修正して"
```

複数行のプロンプトやシェル記号はそのまま渡せるため、シェルエスケープは不要です。

### モデル選択

| エイリアス | 実際のモデル | 用途 |
| --- | --- | --- |
| `codex-spar` | `gpt-5.3-codex-spark` | 高速・局所的なコーディングやレビュー |
| `luna-max` | `gpt-5.6-luna`（`model_reasoning_effort="max"`） | 深い多段推論・難問の解析 |

`--model` にそれ以外の値を渡すと、そのまま Codex に転送されます。ルーティングの
目安は `--list-use-cases`（または `--list-models`）で確認できます。

```sh
./agent-process --list-use-cases
```

### 主なオプション

| オプション | 説明 |
| --- | --- |
| `-m, --model MODEL` | モデルのプリセット名または生のモデル名 |
| `--codex-bin BIN` | Codex 実行ファイル（既定: `codex`） |
| `-C, --cwd DIR` | Codex を実行する作業ディレクトリ |
| `--mode {read-only,write}` / `--write` | アクセスモード（既定: `read-only`） |
| `--persist` | セッションを保持（既定は `--ephemeral`） |
| `--search` | Codex のライブ Web 検索を有効化 |
| `--json` | Codex に `--json` を渡し JSONL イベントを出力 |
| `-o, --output-last-message FILE` | 最終メッセージを `FILE` に書き出す |
| `-i, --image FILE` | 画像を添付（繰り返し可） |
| `--add-dir DIR` | 書き込み可能なディレクトリを追加（繰り返し可） |
| `-p, --profile PROFILE` | Codex の設定プロファイル |
| `-c, --config KEY=VALUE` | Codex の設定上書き（繰り返し可） |
| `--timeout SECONDS` | 待ち時間の上限（既定: 300 秒） |
| `--version` | バージョンを表示 |

### 環境変数

| 変数 | 説明 |
| --- | --- |
| `AGENT_PROCESS_MODEL` | 既定のモデル |
| `AGENT_PROCESS_CODEX_BIN` | 既定の Codex 実行ファイル |
| `AGENT_PROCESS_CWD` | 既定の作業ディレクトリ |
| `AGENT_PROCESS_NESTING` | `1` のとき再帰委譲を拒否 |

## 終了コード

終了コードは契約の一部です。

| コード | 意味 |
| --- | --- |
| `0` | 成功 |
| `2` | プロンプトが与えられなかった（引数なし・対話的 stdin） |
| `123` | 要求された `--output-last-message` が新規作成／変更されなかった |
| `124` | タイムアウト |
| `125` | ネストした実行を拒否 |
| `126` | Codex を実行できない |
| `127` | Codex が見つからない |
| `130` | `SIGINT`（Ctrl-C）で中断 |
| `143` | `SIGTERM` で終了 |

## プロセス／成果物契約

- バックエンドは常に `AGENT_PROCESS_NESTING=1` で起動します。その環境から起動
  されたラッパーは、プロンプトを読む前にネスト実行を拒否し `125` で終了します。
  これは事故防止用のガードであり、セキュリティ境界ではありません。
- すべての呼び出しに有限の待ち期限があります（既定 300 秒）。POSIX では
  バックエンドを新しいプロセスグループ／セッションで起動し、タイムアウトや
  キャンセル時はそのグループだけを終了させます。無関係なセッションや呼び出し元の
  グループは決して終了しません。
- `--output-last-message FILE` は、その実行中に **新規作成または変更された
  通常ファイル** のときだけ成果物として認定します。古い／未変更のファイルは
  `123` で失敗します。タイムアウト・中断・ネスト拒否では成果物を認定しません。
- バックエンドの stdout はそのまま呼び出し元に接続されます（テキストや `--json`
  の JSONL を書き換えません）。stderr はプロセス終了後に転送されます。
- タイムアウト・中断・ネスト拒否・成果物欠落の診断は stderr に、`event`・
  `version`・`run_id`・`model`・`sandbox`・`timeout`・原因などの bounded な
  メタデータ付きで出力されます。プロンプトやソース内容は含まれません。
- バックエンドの非ゼロ終了に使用量制限（usage-limit）のエラーが含まれる場合、
  `event=usage-limit ... limit=5h|weekly|unknown reset_at=...` のような診断が
  stderr に追加されます。

## 開発

```sh
python3 -m unittest discover -s tests -v   # テスト
python3 -m py_compile agent_process.py tests/test_agent_process.py \
    tests/fixtures/fake_codex.py tests/test_install_local.py
```

lint/format ツールの設定はありません。上記テストと `py_compile` が通る状態を
維持してください。テストは `tests/fixtures/fake_codex.py` を `FAKE_CODEX_MODE`
で切り替えて、実際のプロセス境界を検証します。

## スキル

- `.agents/skills/agent-process/` — 委譲エージェント向けの利用スキル
- `.agents/skills/agent-process-install/` — ローカルのインストール／修復スキル

`.claude/skills` は `../.agents/skills` への symlink です。
