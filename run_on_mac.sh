#!/bin/bash

# GEX Suite — macOS 一鍵啟動（venv、依賴、Chromium、main.py）
# 用法：在終端機執行
#   chmod +x run_on_mac.sh
#   ./run_on_mac.sh
#
# 自我更新是「repo-aware」的（見下方），同一支 script 在以下兩種環境都安全：
#   1. 外部使用者 clone 的獨立 GEX_suite repo → 會自我更新（git pull / 抓 zip）
#   2. 開發者的 Jeff-Project monorepo（gex-suite 是子資料夾）→ 跳過自我更新
#      （開發者請手動 pull monorepo；外部發布走 gex-suite/publish-to-public.sh）

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GITHUB_ZIP_URL="https://github.com/chunhua523/GEX_suite/archive/refs/heads/main.zip"
ZIP_EXTRACT_DIR="GEX_suite-main"

echo "========================================"
echo "           GEX Suite (macOS)"
echo "========================================"

# --- 自我更新（repo-aware）---
# 判斷本機環境：
#   repo root == 本 script 所在目錄 → 外部獨立 GEX_suite clone → 自我更新
#   repo root 是上層（Jeff-Project monorepo）或其他 repo → 跳過（避免蓋掉整合版）
#   沒有 .git（解 zip 的非 git 使用者）→ 抓 GEX_suite zip 覆蓋
IS_STANDALONE=false
HAS_GIT=false
if command -v git &> /dev/null && git rev-parse --is-inside-work-tree &> /dev/null; then
    HAS_GIT=true
    if [ "$(git rev-parse --show-toplevel)" = "$SCRIPT_DIR" ]; then
        IS_STANDALONE=true
    fi
fi

if [ "$HAS_GIT" = true ] && [ "$IS_STANDALONE" = false ]; then
    echo "偵測到 monorepo（非獨立 GEX_suite）— 跳過自我更新。"
    echo "（開發者請在 repo 根手動 git pull；外部發布用 gex-suite/publish-to-public.sh）"
elif ping -c 1 github.com &> /dev/null; then
    echo "Checking for updates..."
    if [ "$IS_STANDALONE" = true ]; then
        echo "Git repository detected. Updating via git..."
        if git pull origin main; then
            echo "Git update successful."
        else
            echo "Git update failed — 繼續用現有版本啟動。"
        fi
    else
        echo "Downloading latest version from GitHub..."
        curl -L -o update.zip "$GITHUB_ZIP_URL"
        if [ -f "update.zip" ]; then
            echo "Download complete. Extracting..."
            unzip -q -o update.zip
            if [ -d "$ZIP_EXTRACT_DIR" ]; then
                echo "Applying updates..."
                cp -R "$ZIP_EXTRACT_DIR"/* .
                rm -rf "$ZIP_EXTRACT_DIR"
                rm update.zip
                echo "Update fully applied."
            else
                echo "Error: Update folder structure unexpected (expected $ZIP_EXTRACT_DIR)."
                rm -f update.zip
            fi
        else
            echo "Download failed — 繼續用現有版本啟動。"
        fi
    fi
else
    echo "No internet connection. Skipping update check."
fi

# --- Python 3 ---
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed."
    echo "Please install Python 3.10+ from https://www.python.org/downloads/macos/"
    read -n 1 -s -r -p "Press any key to exit..."
    echo
    exit 1
fi

PYTHON_BIN="python3"
if [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON_BIN="/opt/homebrew/bin/python3"
elif [ -x "/usr/local/bin/python3" ]; then
    PYTHON_BIN="/usr/local/bin/python3"
fi

# requires-python >= 3.10 (pyproject.toml)
if ! "$PYTHON_BIN" - <<'PY' 2>/dev/null
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
then
    echo "Error: Need Python 3.10 or newer (found: $($PYTHON_BIN -V 2>&1))."
    echo "Install from https://www.python.org/downloads/macos/ or: brew install python@3.12"
    read -n 1 -s -r -p "Press any key to exit..."
    echo
    exit 1
fi

# --- venv ---
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (.venv)..."
    "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate

# --- 依賴與 Playwright ---
echo "Checking dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Verifying Playwright Chromium..."
playwright install chromium

# --- 啟動 PySide6 主程式 ---
echo "Starting GEX Suite..."
python main.py
