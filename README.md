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

### ロール開始時間投票

サーバー内の任意のチャンネルで `/schedule add`、`/schedule update`、`/schedule close` を使用でき、
対象には `@VALORANT` 以外も含む任意のロールを指定できます。
`/schedule add` で対象ロールだけを指定した場合、候補には
`20:00 20:30 21:00 21:30 22:00 22:30 23:00 24:00 NG` が自動で入ります。
候補が時刻だけ（末尾の `NG` は指定可）なら自動開始判定が有効になります。`15 16 17`、
`15:00 16:00 17:00`、`1500 1600 1700` は、いずれも表示を
`15:00 16:00 17:00` に統一します。早い時刻側から投票者の重複を除いて累計し、5人目が
加わった時刻が24:00なら、最後の対象リアクション変更から10秒待って再集計し、Botが
`24:00 開始 @ロール` のように通知します。10秒以内に投票が変わった場合は待ち時間を
最初から数え直すため、誤タップを戻せば通知されません。
集計対象は対象ロールの所属有無にかかわらず、その投票へ反応したBot以外のユーザーです。

通知後もリアクションの追加・削除・全消去を監視します。開始時刻が変わった場合や5人未満に
なった場合は、10秒待ってから以前の通知を取り消し線付きに編集し、変更または取消を新しい
メッセージで知らせます。個別のリアクション解除で5人未満になった場合は、最後に解除した
ユーザー名を表示します（一括削除など個人を特定できない場合は人数不足として表示）。
リアクション操作によるメンション連打を防ぐため、対象ロールへの
通知は最初の開始通知だけで、以後の変更・取消メッセージではロール名を表示しても再通知しません。
有効な時刻形式の投票は `json/schedule_polls.sqlite3` に記録され、Bot再起動後も10秒待って
再集計します。
登録から約90日（3か月）を過ぎた投票は、Bot起動時または新しい投票の作成時にSQLiteの
登録だけを削除して自動判定を終了します。Discord上の投票投稿と通知は削除しません。
3か月を待たずに使い終わった投票は `/schedule close` で終了すると、古い投票による将来の
誤通知を防げます。
`json/` はBotから書き込め、再起動後も残るディスクに置いてください。通知の重複を避けるため、
Botは1プロセス（1レプリカ）での運用を前提とします。

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
