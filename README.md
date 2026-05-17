# Kazekoshi v3.0

Discord読み上げBot。VOICEVOX + Gemini AI搭載。

## 必要なAPIキー（すべて無料）

| サービス | 用途 | 取得先 |
|---|---|---|
| Discord Bot Token | Bot本体 | [Discord Developer Portal](https://discord.com/developers/applications) |
| OpenWeatherMap | 天気機能 | [openweathermap.org](https://openweathermap.org/api) |
| Google Gemini API | AI会話 | [Google AI Studio](https://aistudio.google.com/apikey) |

## ローカル起動

```bash
pip install -r requirements.txt
cp config.ini.example config.ini
# config.ini を編集して各APIキーを設定
python Kazekoshi.py
```

## AWS EC2 デプロイ（無料枠）

> **注意**: AWS Free Tierは新規アカウントから**12ヶ月のみ**無料。  
> t2.micro（1 vCPU / 1GB RAM）が対象インスタンス。

### 1. EC2インスタンスを起動

1. AWSコンソール → EC2 → 「インスタンスを起動」
2. AMI: **Ubuntu 22.04 LTS**
3. インスタンスタイプ: **t2.micro**（無料枠対象）
4. セキュリティグループ: SSH（ポート22）のみ許可
5. キーペアを作成してダウンロード

### 2. サーバーにSSH接続

```bash
chmod 400 your-key.pem
ssh -i your-key.pem ubuntu@<EC2のパブリックIP>
```

### 3. 環境セットアップ

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv ffmpeg git

git clone https://github.com/0219angry/Kazekoshi3.0.git
cd Kazekoshi3.0

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. VOICEVOX辞書のインストール

```bash
# Open JTalk辞書をダウンロード
wget https://github.com/r9y9/open_jtalk/releases/download/v1.11.1/open_jtalk_dic_utf_8-1.11.tar.gz
tar xzf open_jtalk_dic_utf_8-1.11.tar.gz
```

### 5. config.ini を作成

```bash
cp config.ini.example config.ini
nano config.ini   # 各APIキーを入力
```

### 6. systemdで常時起動設定

```bash
sudo nano /etc/systemd/system/kazekoshi.service
```

以下を貼り付け（パスを環境に合わせて変更）:

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

# ログ確認
sudo systemctl status kazekoshi
journalctl -u kazekoshi -f
```

## コスト試算（AWS）

| 期間 | 費用 |
|---|---|
| 最初の12ヶ月 | **$0**（Free Tier） |
| 12ヶ月以降 | 約 $8〜10/月（t2.micro オンデマンド） |

12ヶ月以降も無料で使い続けたい場合は **Oracle Cloud Free Tier**（ARM VM 4コア/24GB、期間無制限）への移行を推奨。
