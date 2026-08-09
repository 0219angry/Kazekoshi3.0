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

### VALORANT開始時間投票の設定

`config.ini` の `VALORANT_CHANNEL_ID` に `#valorant` のチャンネルIDを設定すると、
`/schedule add` と `/schedule update` をそのチャンネルだけで使えます。`0` のままなら
チャンネル名 `valorant` で判定します。
`/schedule add` で対象ロールだけを指定した場合、候補には
`20 21 22 23 24 ng` が自動で入ります。
この省略形式では `ng` を除外し、20時側から投票者の重複を除いて累計します。5人目が
加わった時刻が24なら、Botが `24時開始 @VALORANT` のように一度だけ通知します。候補を明示した
場合と `/schedule update` 後は、この自動通知の対象外です。

Botには **チャンネルを見る・メッセージを送信・埋め込みリンク・メッセージ履歴を読む・
リアクションを追加** の各権限が必要。`/schedule update` で既存票をリセットするには、
追加で **メッセージの管理** 権限が必要。Botを再起動するとDiscordへスラッシュコマンドを
同期します。対象ロールはメンション可能であること。
メンション不可のロールを使う場合は実行者に **@everyone、@here、すべてのロールに
メンション** 権限が必要。スラッシュコマンド経由、または候補省略の自動開始通知を
使う場合は、Botにも同じ権限が必要。

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
