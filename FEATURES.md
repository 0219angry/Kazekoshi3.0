# Kazekoshi v3.0 機能一覧

## アーキテクチャ

```
Kazekoshi.py          # エントリーポイント・Bot本体
kazekoshi/
└── cogs/
    ├── voice.py      # 読み上げ（VOICEVOX）
    ├── dice.py       # ダイス・コイン
    ├── notify.py     # VC入室通知
    ├── weather.py    # 天気情報
    ├── dictionary.py # 読み上げ辞書
    ├── ai_chat.py    # AI会話（Claude API）
    ├── poll.py       # 投票
    ├── reminder.py   # リマインダー
    └── utility.py    # ユーティリティ
log/                  # ログファイル（自動作成・最大5件）
temp/                 # 音声ファイルの一時置き場（自動作成・最大10件）
json/                 # サーバーごとのデータ（自動作成）
config.ini            # 設定ファイル（config.ini.example をコピーして作成）
```

---

## 機能詳細

### 🎤 読み上げ（VOICEVOX）
`kazekoshi/cogs/voice.py`

| コマンド | 説明 |
|---|---|
| `/join` | ユーザーが接続中のVCに参加し、テキストチャンネルの発言を読み上げ開始 |
| `/leave` | VCから切断し、読み上げを終了 |
| `/voice` | プルダウンメニューで自分の読み上げボイスを変更（VOICEVOX話者から選択） |

- URL・メンション・チャンネルリンクを自動的に読みやすい形に変換
- 100文字を超えるメッセージは「以下省略」でカット
- VCに自分だけが残ると自動切断
- ユーザーごとのボイス設定はサーバー単位でJSONに永続化

---

### 🎲 ダイス・ゲーム
`kazekoshi/cogs/dice.py`

| コマンド | 説明 |
|---|---|
| `/dice [表記]` | NdM形式でダイスを振る（例: `2d6`、`1d20`）。個数1〜100、面数2〜10000 |
| `/coin` | コインを投げて表・裏を返す |

---

### 🔔 VC入室通知
`kazekoshi/cogs/notify.py`

| コマンド | 説明 |
|---|---|
| `/notify` | 接続中のVCへの入室を指定テキストチャンネルに通知するよう設定 |
| `/notify_remove` | 接続中のVCの通知設定を解除 |
| `/notify_list` | サーバーの通知設定一覧を表示 |

- 通知は「最初の1人が入室したとき」のみ（既に誰かいる場合は通知しない）
- 設定はサーバー単位でJSONに永続化

---

### 🌤️ 天気情報
`kazekoshi/cogs/weather.py`

| コマンド / トリガー | 説明 |
|---|---|
| `/weather [都市名]` | 指定都市の天気を表示（省略時はconfig.iniのデフォルト拠点） |
| `あつい` / `あつくない` | 現在気温（25℃基準）に合わせてリアクション |
| `さむい` / `さむくない` | 現在気温（12℃基準）に合わせてリアクション |

- OpenWeatherMap API を使用
- 表示項目: 天気・気温・体感温度・最低/最高気温・湿度・風速

---

### 📖 読み上げ辞書
`kazekoshi/cogs/dictionary.py`

| コマンド | 説明 |
|---|---|
| `/dict_add [単語] [読み]` | 読み上げ辞書に単語を追加（例: `草` → `くさ`） |
| `/dict_del [単語]` | 辞書から単語を削除 |
| `/dict_list` | 辞書の内容を一覧表示（最大25件） |

- 辞書はサーバー単位でJSONに永続化
- 読み上げ時に自動適用される

---

### 🤖 AI会話（Claude API）
`kazekoshi/cogs/ai_chat.py`

| コマンド / トリガー | 説明 |
|---|---|
| `/ai [メッセージ]` | Claudeと会話（会話履歴あり） |
| `/ai_reset` | 自分の会話履歴をリセット |
| `@Bot メンション` | メンションでAIに直接話しかける |

- モデル: `claude-sonnet-4-6`
- 会話履歴はユーザーごとにメモリ上で管理（最大20ターン）
- `ANTHROPIC_API_KEY` が未設定の場合は機能が無効化される

---

### 📊 投票
`kazekoshi/cogs/poll.py`

| コマンド | 説明 |
|---|---|
| `/poll [質問] [選択肢]` | 最大10択の投票を作成（選択肢はカンマ区切り） |
| `/quickpoll [質問]` | 👍 / 👎 の簡易投票を作成 |

---

### ⏰ リマインダー
`kazekoshi/cogs/reminder.py`

| コマンド | 説明 |
|---|---|
| `/remind [時間] [内容]` | 指定時間後にメンションでリマインド |

- 時間指定: `30s`（秒）・`10m`（分）・`2h`（時間）・`1d`（日）・組み合わせ可（`1h30m`）
- 最小10秒、最大7日
- ボットを再起動するとリマインダーは消える（永続化なし）

---

### 🛠️ ユーティリティ
`kazekoshi/cogs/utility.py`

| コマンド | 説明 |
|---|---|
| `/ping` | BotのWebSocketレイテンシを表示 |
| `/userinfo [メンバー]` | ユーザー情報を表示（省略で自分） |
| `/serverinfo` | サーバー情報を表示 |
| `/avatar [メンバー]` | アバター画像を表示（省略で自分） |
| `/choose [選択肢]` | カンマ区切りの選択肢からランダムに1つ選ぶ |
| `/help` | コマンド一覧を表示（ephemeral） |

- 「就活」関連の発言を検出して自動反応

---

### 🎵 音楽再生
`kazekoshi/cogs/music.py`

| コマンド | 説明 |
|---|---|
| `!play <曲名/URL>` / `!p` | YouTube検索・再生。キューに曲を追加 |
| `!play <プレイリストURL>` | YouTubeプレイリストをまるごとキューに追加 |
| `!skip` / `!s` | 現在の曲をスキップ（ループ中でも強制スキップ） |
| `!stop` | 再生停止・VC切断・キュークリア |
| `!pause` | 一時停止 |
| `!resume` | 再開 |
| `!queue` / `!q` | キュー一覧表示（最大10件） |
| `!nowplaying` / `!np` | 再生中の曲を表示 |
| `!loop` / `!l` | 1曲ループ ON/OFF |
| `!loop queue` | キューループ ON/OFF |
| `!shuffle` | キューをシャッフル |
| `!volume <0-100>` / `!vol` | 音量調整（デフォルト50%） |
| `!remove <番号>` / `!rm` | キューから指定番号の曲を削除 |
| `!history` / `!hist` | 再生履歴を表示（最新20件） |
| `!playlist save <名前>` | 現在のキューをプレイリストとして保存 |
| `!playlist load <名前>` | 保存済みプレイリストをキューに追加 |
| `!playlist list` | 保存済みプレイリスト一覧 |
| `!playlist delete <名前>` | プレイリストを削除 |

- VCに誰もいなくなると自動切断
- プレイリストはサーバー単位でJSONに永続化

---

## セットアップ

```bash
# 1. 依存パッケージのインストール
pip install -r requirements.txt

# 2. 設定ファイルの作成
cp config.ini.example config.ini
# config.ini を編集して各APIキーを設定

# 3. 起動
python Kazekoshi.py
```

### config.ini の主な設定項目

| キー | 説明 |
|---|---|
| `DISCORD_TOKEN` | Discord Bot のトークン（必須） |
| `COMMAND_PREFIX` | プレフィックス（デフォルト: `!`） |
| `SPEAKER_ID` | VOICEVOXのデフォルト話者ID（デフォルト: `3` = ずんだもんノーマル） |
| `OPEN_JTALK_DICT_DIR` | Open JTalk 辞書のパス |
| `OPEN_WEATHER_MAP_TOKEN` | OpenWeatherMap API キー（天気機能に必要） |
| `DEFAULT_LAT` / `DEFAULT_LON` | デフォルト拠点の座標 |
| `DEFAULT_CITY` | デフォルト拠点の都市名 |
| `ANTHROPIC_API_KEY` | Anthropic API キー（AI機能に必要、未設定で無効化） |
