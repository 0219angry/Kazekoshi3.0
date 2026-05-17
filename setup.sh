#!/bin/bash
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

TOTAL=4
step() { echo -e "\n${YELLOW}[${1}/${TOTAL}] ${2}${NC}"; }
ok()   { echo -e "${GREEN}✓ ${1}${NC}"; }

cd "$(dirname "${BASH_SOURCE[0]}")"

echo -e "${BOLD}=== Kazekoshi v3.0 セットアップ ===${NC}"

# ─── 1. ffmpeg ──────────────────────────────────────────────────────
step 1 "ffmpeg のインストール"
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg は既にインストール済み"
else
    sudo apt-get update -qq && sudo apt-get install -y ffmpeg wget
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

# voicevox-core は PyPI に存在しないため GitHub releases から取得する
ARCH=$(uname -m)
PY_VER=$(venv/bin/python -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')")
case "$ARCH" in
    x86_64)  WHEEL_ARCH="linux_x86_64" ;;
    aarch64) WHEEL_ARCH="linux_aarch64" ;;
    *) echo "未対応アーキテクチャ: $ARCH"; exit 1 ;;
esac
echo "  アーキテクチャ: ${ARCH} → GitHubからvoicevox-coreを取得します..."
LATEST=$(curl -fsSL "https://api.github.com/repos/VOICEVOX/voicevox_core/releases/latest" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
WHEEL="voicevox_core-${LATEST}+cpu-cp${PY_VER}-cp${PY_VER}-${WHEEL_ARCH}.whl"
venv/bin/pip install -q \
    "https://github.com/VOICEVOX/voicevox_core/releases/download/${LATEST}/${WHEEL}"
ok "Pythonパッケージをインストールしました"

# ─── 3. Open JTalk辞書 ──────────────────────────────────────────────
step 3 "Open JTalk辞書のダウンロード"
DICT_DIR="open_jtalk_dic_utf_8-1.11"
if [ -d "$DICT_DIR" ]; then
    ok "辞書は既に存在します"
else
    wget --show-progress -q \
        "https://github.com/r9y9/open_jtalk/releases/download/v1.11.1/open_jtalk_dic_utf_8-1.11.tar.gz"
    tar xzf open_jtalk_dic_utf_8-1.11.tar.gz
    rm open_jtalk_dic_utf_8-1.11.tar.gz
    ok "辞書をダウンロードしました"
fi

# ─── 4. config.ini ──────────────────────────────────────────────────
step 4 "設定ファイルの作成"
if [ -f "config.ini" ]; then
    ok "config.ini は既に存在します（スキップ）"
else
    cp config.ini.example config.ini

    echo -e "\n${BLUE}APIキーを入力してください（Enterでスキップして後でconfig.iniを編集することも可）${NC}"
    echo ""

    read -rp "  Discord Bot Token（必須）: " discord_token
    read -rp "  OpenWeatherMap API Key  : " owm_token
    read -rp "  Gemini API Key          : " gemini_key

    # | をデリミタにして / を含むトークンでも安全に置換
    [ -n "$discord_token" ] && sed -i "s|your_discord_bot_token_here|${discord_token}|g" config.ini
    [ -n "$owm_token" ]     && sed -i "s|your_openweathermap_api_key_here|${owm_token}|g" config.ini
    [ -n "$gemini_key" ]    && sed -i "s|your_gemini_api_key_here|${gemini_key}|g" config.ini

    ok "config.ini を作成しました"
fi

# ─── 完了 ───────────────────────────────────────────────────────────
echo -e "\n${GREEN}${BOLD}=== セットアップ完了！ ===${NC}"
echo -e "\n起動コマンド:"
echo -e "  ${BOLD}source venv/bin/activate && python Kazekoshi.py${NC}\n"
