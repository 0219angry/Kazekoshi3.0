# Kazekoshi v3.0

Discord読み上げBot。VOICEVOX + Gemini AI搭載。

## 必要なAPIキー（すべて無料）

| サービス | 用途 | 取得先 |
|---|---|---|
| Discord Bot Token | Bot本体 | [Discord Developer Portal](https://discord.com/developers/applications) |
| OpenWeatherMap | 天気機能 | [openweathermap.org](https://openweathermap.org/api) |
| Google Gemini API | AI会話 | [Google AI Studio](https://aistudio.google.com/apikey) |

## セットアップ（ローカル・サーバー共通）

`setup.sh` が ffmpeg・仮想環境・パッケージ・辞書・config.ini の作成をすべて自動で行います。

```bash
bash setup.sh
```

実行後は以下で起動:

```bash
source venv/bin/activate && python Kazekoshi.py
```

### 開始時間投票（`/schedule`）

任意のチャンネル・ロールで利用できます。

```text
/schedule add @ロール
/schedule add @ロール 15 16 17 NG [3]
```

- デフォルト候補: `20:00 20:30 21:00 21:30 22:00 22:30 23:00 24:00 NG`
- 最低人数: 5人。末尾の `[3]` のような指定で変更可能
- 時刻表記: `15`、`15:00`、`1500` はすべて `15:00` に統一。`NG` は大文字・小文字どちらでも可
- 自動判定: 時刻順に重複を除いて集計し、最低人数に達すると8秒後に開始時刻を通知

#### 主なコマンド

`message`（投稿IDまたはリンク）を省略すると、同じチャンネルの最新投票を対象にします。

| コマンド | 内容 |
|---|---|
| `/schedule add` | 投票を作成 |
| `/schedule status` | 票数・累計人数・成立時刻を表示 |
| `/schedule clone` | ロール・候補・最低人数を複製（誰でも実行可） |
| `/schedule date` | 開催日を設定。`YYYY-MM-DD`、`YYYYMMDD`、`MM-DD`、`MMDD`、`DD` に対応。解除は `clear` |
| `/schedule minimum` | 最低人数を変更 |
| `/schedule deadline` | `YYYY-MM-DD HH:MM` で自動終了時刻を設定。解除は `clear` |
| `/schedule update` | 候補を変更し、投票をリセット |
| `/schedule decide` | 候補から開始時刻を手動確定 |
| `/schedule late` | 月次・年次の遅刻集計とグラフを表示 |
| `/schedule lateoff` | 遅刻の記録・集計・通知を停止 |
| `/schedule close` | 投票と自動判定を終了 |
| `/schedule delete` | 投稿と関連データを完全削除 |

#### 遅刻判定

- 開始15分前の参加リアクションを対象に、開始後に同じVCへ最低人数−2人（最低1人）が集まると記録を開始
- 未到着者には15・30・60・120分後に通知し、遅刻は1募集につき最大180分で集計
- 参加予定者が最低人数そろうと判定終了。リアクションを外した人は対象外
- `/schedule late` の期間指定: 省略で今月、`10` で今年10月、`2026` で年全体、`202704` または `2027-04` で年月指定

投票の変更・終了・削除は、作成者またはメッセージ管理権限を持つ人だけが実行できます。
Botには、チャンネル閲覧・送信・埋め込み・履歴閲覧・リアクション追加の権限が必要です。
状態は `json/` に保存されるため、永続ディスクを使用し、Botは1プロセスで運用してください。

#### 風越RUSHの設定

`config.ini` に次の項目を追加します。

```ini
[SCHEDULE_EFFECT]
ENABLED = true
USER_IDS = 123456789012345678, 987654321098765432
DELETE_AFTER_SECONDS = 8
```

| 項目 | 設定内容 |
|---|---|
| `ENABLED` | 有効にする場合は `true` |
| `USER_IDS` | 対象ユーザーIDをカンマまたは空白で区切って指定 |
| `DELETE_AFTER_SECONDS` | 演出メッセージを削除するまでの秒数 |

## Oracle Cloud Free Tier デプロイ（永久無料）

> Oracle Cloud の **Always Free** プランは期間制限なしで永久に無料。  
> ARM Ampere A1インスタンス（4コア/24GB RAM）が使えるためVOICEVOXも余裕で動く。  
> 登録にクレジットカードが必要だが、Free Tierの範囲では課金されない。

### 1. アカウント作成とインスタンス起動

1. [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) でアカウント登録
2. コンソール → **コンピュート** → **インスタンスの作成**
3. 以下の設定にする:
   - イメージ: **Ubuntu 22.04**
   - シェイプ: **Ampere A1**（`VM.Standard.A1.Flex`）
   - OCPU: 4、メモリ: 24GB（Always Free枠の上限）
4. SSHキーを作成してダウンロード
5. インスタンスを作成

### 2. SSHで接続

```bash
chmod 400 your-key.pem
ssh -i your-key.pem ubuntu@<インスタンスのパブリックIP>
```

### 3. リポジトリのクローンとセットアップ

```bash
sudo apt install -y git python3-venv
git clone https://github.com/0219angry/Kazekoshi3.0.git
cd Kazekoshi3.0
bash setup.sh
```

### 4. systemdで常時起動設定

```bash
sudo nano /etc/systemd/system/kazekoshi.service
```

以下を貼り付け:

```ini
[Unit]
Description=Kazekoshi Discord Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/Kazekoshi3.0
ExecStart=/home/ubuntu/Kazekoshi3.0/venv/bin/python Kazekoshi.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable kazekoshi
sudo systemctl start kazekoshi

# 動作確認
sudo systemctl status kazekoshi

# ログをリアルタイムで見る
journalctl -u kazekoshi -f
```

## コスト

| 項目 | 費用 |
|---|---|
| Oracle Cloud VM（ARM 4コア/24GB） | **$0（永久無料）** |
| Discord Bot Token | $0 |
| OpenWeatherMap | $0（月100万回まで） |
| Gemini API | $0（1日1500回まで） |
| **合計** | **$0** |
