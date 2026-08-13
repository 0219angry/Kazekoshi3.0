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

サーバー内の任意のチャンネルで `/schedule add`、`/schedule status`、`/schedule clone`、`/schedule date`、`/schedule minimum`、`/schedule deadline`、`/schedule update`、`/schedule decide`、`/schedule lateoff`、`/schedule close` を使用でき、
対象には `@VALORANT` 以外も含む任意のロールを指定できます。
`add` 以外は投稿ID・リンクを省略でき、省略時は同じチャンネルの直近100件から最新の
開始時間投票を対象にします。
`/schedule add` で対象ロールだけを指定した場合、候補には
`20:00 20:30 21:00 21:30 22:00 22:30 23:00 24:00 NG` が自動で入ります。
最低人数はデフォルトで5人です。コマンドの末尾に `[3]` のように1〜999人で指定すると、その投票だけ
最低人数を変更できます（例: `/schedule add @ロール 15 16 17 NG [3]`）。候補を省略して
デフォルト時刻を使う場合は `/schedule add @ロール [3]` と指定できます。作成後は
`/schedule minimum message:<投稿IDまたはリンク> minimum:3` で、既存票を残したまま変更できます。投票タイトルには
`開始時間 [5人]` または `開始時間 [3人]` のように、実際の最低人数を表示します。
候補が時刻だけ（末尾の `NG` は指定可）なら自動開始判定が有効になります。`15 16 17`、
`15:00 16:00 17:00`、`1500 1600 1700` は、いずれも表示を
`15:00 16:00 17:00` に統一します。早い時刻側から投票者の重複を除いて累計し、累計が指定人数に
達したら、最後の対象リアクション変更から10秒待って再集計し、Botが
`24:00 開始 @ロール` のように通知します。10秒以内に投票が変わった場合は待ち時間を
最初から数え直すため、誤タップを戻せば通知されません。
集計対象は対象ロールの所属有無にかかわらず、その投票へ反応したBot以外のユーザーです。
`/schedule status message:<投稿IDまたはリンク>` では、候補ごとの票数、時刻順に重複を除いた
累計人数、現在の成立時刻を確認できます。スラッシュコマンドでは結果を実行者だけに表示します。
`/schedule clone message:<投稿IDまたはリンク>` は誰でも実行でき、元投票のロール・候補・最低人数で
新しい投票を作ります。既存票・締切・開始通知・終了状態は引き継ぎません。
開催日は通常、投票を投稿した日の日本時間です。別日の募集は
`/schedule date message:<投稿IDまたはリンク> date:2026-08-14` で今日から前後90日以内の日付を設定し、
`clear` で投稿日へ戻せます。
`/schedule deadline` では、日本時間の `YYYY-MM-DD HH:MM` 形式で90日以内の締切を設定できます。
締切は再起動後も復元され、時刻になると開始通知を取り消して投票を自動終了します。`clear` または
`解除` を指定すると締切を取り消せます。
`/schedule decide message:<投稿IDまたはリンク> time:21:00` では、作成者またはメッセージ管理権限を
持つ人が候補から開始時刻を手動確定できます。確定通知を送って投票を終了し、既に開始通知済みなら
ロールを再メンションしません。

開始通知または手動確定がある募集は、開始15分前になった時点で、確定時刻以前の候補へ
リアクションしたユーザーを遅刻判定メンバーとして固定します。それ以降に初めてリアクションした
ユーザーは追加しません。ただし、固定後に対象リアクションをすべて外したユーザーはキャンセル扱いで
統計と将来の遅刻通知から除外し、開始前後を問わず再度リアクションすれば対象へ戻します。
開始時刻を過ぎ、固定メンバーが同じVCに「募集の最低人数−2人」以上そろうと、そのVCでの開催と
判断します（最低1人。例: 最低5人なら3人）。開始から3時間以内にそのVCへ初めて入った
固定メンバーについて、開始時刻からの遅れをSQLiteへ記録します。3時間経過時点の未入室者は
欠席として180分の遅刻を記録し、1回の募集で集計する遅刻は最大180分です。
Bot再起動時点ですでにVCにいる人は、誤って遅刻扱いにしないため開始時刻からいたものと
みなします。VC開催を認識した後は、統計との食い違いを防ぐため開始時刻・開催日・候補・最低人数を
変更できません。遅刻中の通知はまだ送信せず、記録だけを行います。
遅刻判定が不要な募集や、途中で判定を止めたい募集は
`/schedule lateoff message:<投稿IDまたはリンク>` で停止できます。作成者またはメッセージ管理権限を
持つ人だけが実行でき、投稿指定の省略時は同チャンネルの最新募集が対象です。停止後は記録を増やさず、
すでに記録済みの分も含めてその募集を月次統計と将来の遅刻通知から除外します。

通知後もリアクションの追加・削除・全消去を監視します。開始時刻が変わった場合や指定人数未満に
なった場合は、10秒待ってから以前の通知を取り消し線付きに編集し、変更または取消を新しい
メッセージで知らせます。個別のリアクション解除で指定人数未満になった場合は、最後に解除した
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
