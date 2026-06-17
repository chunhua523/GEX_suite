#!/bin/bash

# GEX Suite — macOS 一鍵啟動（venv、依賴、Chromium、main.py）
# 用法：在終端機執行
#   chmod +x run_on_mac.sh
#   ./run_on_mac.sh
#
# 若 GitHub 儲存庫名稱或帳號不同，請改下方 GITHUB_ZIP_URL。

cd "$(dirname "$0")"

GITHUB_ZIP_URL="https://github.com/chunhua523/GEX_suite/archive/refs/heads/main.zip"
ZIP_EXTRACT_DIR="GEX_suite-main"

echo "========================================"
echo "           GEX Suite (macOS)"
echo "========================================"

# --- 可選：檢查網路並嘗試更新 ---
echo "Checking internet connection..."
if ping -c 1 github.com &> /dev/null; then
    echo "Connected."

    UPDATED=false

    if [ -d ".git" ] && command -v git &> /dev/null; then
        echo "Git repository detected. Updating via git..."
        git pull origin main
        if [ $? -eq 0 ]; then
            UPDATED=true
            echo "Git update successful."
        else
            echo "Git update failed. Trying direct download..."
        fi
    fi

    if [ "$UPDATED" = false ]; then
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
            echo "Download failed."
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
