#!/bin/bash
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

TOTAL=5
step() { echo -e "\n${YELLOW}[${1}/${TOTAL}] ${2}${NC}"; }
ok()   { echo -e "${GREEN}✓ ${1}${NC}"; }

cd "$(dirname "${BASH_SOURCE[0]}")"
echo -e "${BOLD}=== Kazekoshi v3.0 セットアップ ===${NC}"

# ─── 1. ffmpeg ──────────────────────────────────────────────────────
step 1 "ffmpeg のインストール"
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg は既にインストール済み"
else
    sudo apt-get update -qq && sudo apt-get install -y ffmpeg wget unzip
    ok "ffmpeg をインストールしました"
fi

# ─── 2. Python仮想環境 & パッケージ ─────────────────────────────────
step 2 "Python仮想環境 & パッケージのインストール"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    ok "仮想環境を作成しました"
else
    ok "仮想環境は既に存在します"
fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -r requirements.txt

ARCH=$(uname -m)
PY_VER=$(venv/bin/python -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')")
echo "  アーキテクチャ: ${ARCH} → voicevox-core を検索中..."

# GitHub API（api.github.com）を使わずリリースページのリダイレクト先からバージョンを取得
# → レート制限・認証不要
LATEST=$(curl -fsSL -A "Kazekoshi-setup/3.0" -o /dev/null -w "%{url_effective}" \
    "https://github.com/VOICEVOX/voicevox_core/releases/latest" \
    | grep -oE '[^/]+$') || {
    echo "❌ バージョン取得に失敗しました（ネットワークを確認してください）"; exit 1
}
echo "  最新バージョン: ${LATEST}"

# expanded_assets ページから実際のホイールURLを取得（API不要・ファイル名推測不要）
WHEEL_URL=$(python3 - <<PYEOF
import urllib.request, re, sys
tag  = "${LATEST}"
arch = "${ARCH}"
pyver = "${PY_VER}"
url = f"https://github.com/VOICEVOX/voicevox_core/releases/expanded_assets/{tag}"
req = urllib.request.Request(url, headers={"User-Agent": "Kazekoshi-setup/3.0"})
try:
    html = urllib.request.urlopen(req).read().decode()
    # manylinux & arch一致 & .whl で終わるassetリンクを取得（macOS等を除外）
    all_whl = re.findall(r'href="(/[^"]*manylinux[^"]*' + re.escape(arch) + r'[^"]*\.whl)"', html)
    # 現在のPython版に一致するものを優先、なければ最初の候補
    matched = [u for u in all_whl if f"cp{pyver}" in u or "py3" in u or "abi3" in u]
    best = matched[0] if matched else (all_whl[0] if all_whl else "")
    print("https://github.com" + best if best else "")
except Exception as e:
    print("", file=sys.stderr)
    sys.exit(1)
PYEOF
)

if [ -z "$WHEEL_URL" ]; then
    echo "❌ voicevox-core の対応ホイールが見つかりませんでした（tag=${LATEST}, ARCH=${ARCH}）"
    exit 1
fi
echo "  ホイール: $(basename "${WHEEL_URL}")"
venv/bin/pip install -q "${WHEEL_URL}" || {
    echo "❌ voicevox-core のインストールに失敗しました"
    echo "   試したURL: ${WHEEL_URL}"
    exit 1
}
ok "Pythonパッケージをインストールしました"

# ─── 3. VOICEVOXダウンローダー & ランタイム & 音声モデル ────────────
step 3 "VOICEVOXランタイム & 音声モデルのダウンロード"
mkdir -p lib models
case "$ARCH" in
    x86_64)  DL_BIN="download-linux-x64" ;;
    aarch64) DL_BIN="download-linux-arm64" ;;
    *) echo "❌ 未対応アーキテクチャ: $ARCH"; exit 1 ;;
esac

# ダウンローダーバイナリを取得
if [ ! -f "$DL_BIN" ]; then
    wget -q --user-agent="Kazekoshi-setup/3.0" \
        "https://github.com/VOICEVOX/voicevox_core/releases/download/${LATEST}/${DL_BIN}"
    chmod +x "$DL_BIN"
fi

# ONNXランタイム（利用規約: 非商用・商用OK、クレジット表記必要）
LIB_FILE=$(venv/bin/python -c "from voicevox_core.blocking import Onnxruntime; print(Onnxruntime.LIB_VERSIONED_FILENAME)")
if ls lib/*.so* &>/dev/null 2>&1; then
    ok "ONNXランタイムは既に存在します"
else
    echo "  ONNXランタイムをダウンロード中..."
    echo "y" | ./"$DL_BIN" --only onnxruntime -o voicevox_dl_tmp 2>&1 | grep -v "^\[" || true
    find voicevox_dl_tmp/ -name "*.so*" -exec cp {} lib/ \;
    rm -rf voicevox_dl_tmp
    ok "ONNXランタイムをインストールしました"
fi

# 音声モデル（利用規約: キャラクターごとに確認が必要）
if ls models/*.vvm &>/dev/null 2>&1; then
    ok "音声モデルが存在します: $(ls models/*.vvm | wc -l) 件"
else
    echo "  音声モデルをダウンロード中（数百MB・時間がかかります）..."
    echo "y" | ./"$DL_BIN" --only models -o voicevox_dl_tmp 2>&1 | grep -v "^\[" || true
    find voicevox_dl_tmp/ -name "*.vvm" -exec cp {} models/ \;
    rm -rf voicevox_dl_tmp
    ok "音声モデルをダウンロードしました: $(ls models/*.vvm | wc -l) 件"
fi

# ─── 4. Open JTalk辞書 ──────────────────────────────────────────────
step 4 "Open JTalk辞書のダウンロード"
if [ -d "open_jtalk_dic_utf_8-1.11" ]; then
    ok "辞書は既に存在します"
else
    wget --show-progress -q \
        "https://github.com/r9y9/open_jtalk/releases/download/v1.11.1/open_jtalk_dic_utf_8-1.11.tar.gz"
    tar xzf open_jtalk_dic_utf_8-1.11.tar.gz
    rm open_jtalk_dic_utf_8-1.11.tar.gz
    ok "辞書をダウンロードしました"
fi

# ─── 5. config.ini ──────────────────────────────────────────────────
step 5 "設定ファイルの作成"
if [ -f "config.ini" ]; then
    ok "config.ini は既に存在します（スキップ）"
else
    cp config.ini.example config.ini
    echo -e "\n${BLUE}APIキーを入力してください（Enterでスキップ・後でconfig.iniを直接編集も可）${NC}\n"
    read -rp "  Discord Bot Token（必須）: " discord_token
    read -rp "  OpenWeatherMap API Key  : " owm_token
    read -rp "  Gemini API Key          : " gemini_key
    [ -n "$discord_token" ] && sed -i "s|your_discord_bot_token_here|${discord_token}|g" config.ini
    [ -n "$owm_token" ]     && sed -i "s|your_openweathermap_api_key_here|${owm_token}|g" config.ini
    [ -n "$gemini_key" ]    && sed -i "s|your_gemini_api_key_here|${gemini_key}|g" config.ini
    ok "config.ini を作成しました"
fi

# ─── run.sh ─────────────────────────────────────────────────────────
cat > run.sh << 'EOF'
#!/bin/bash
cd "$(dirname "${BASH_SOURCE[0]}")"
export LD_LIBRARY_PATH="$PWD/lib:${LD_LIBRARY_PATH:-}"
exec venv/bin/python Kazekoshi.py "$@"
EOF
chmod +x run.sh

# ─── 完了 ───────────────────────────────────────────────────────────
echo -e "\n${GREEN}${BOLD}=== セットアップ完了！ ===${NC}"
echo -e "\n起動コマンド:"
echo -e "  ${BOLD}bash run.sh${NC}\n"
