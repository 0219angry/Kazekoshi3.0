#!/bin/bash
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

TOTAL=3
step() { echo -e "\n${YELLOW}[${1}/${TOTAL}] ${2}${NC}"; }
ok()   { echo -e "${GREEN}✓ ${1}${NC}"; }

cd "$(dirname "${BASH_SOURCE[0]}")"
echo -e "${BOLD}=== Kazekoshi v3.0 セットアップ ===${NC}"

# ─── 1. ffmpeg ──────────────────────────────────────────────────────
step 1 "ffmpeg のインストール"
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg は既にインストール済み"
else
    sudo apt-get update -qq && sudo apt-get install -y ffmpeg
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
ok "Pythonパッケージをインストールしました"

# ─── 3. config.ini ──────────────────────────────────────────────────
step 3 "設定ファイルの作成"
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
exec venv/bin/python Kazekoshi.py "$@"
EOF
chmod +x run.sh

# ─── 完了 ───────────────────────────────────────────────────────────
echo -e "\n${GREEN}${BOLD}=== セットアップ完了！ ===${NC}"
echo -e "\n起動コマンド:"
echo -e "  ${BOLD}bash run.sh${NC}\n"
