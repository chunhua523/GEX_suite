#!/bin/bash
# 把 Jeff-Project monorepo 的 gex-suite/ 子資料夾，鏡像發布到 public repo
#   https://github.com/chunhua523/GEX_suite （外部使用者自動更新來源）
#
# 開發在 monorepo（private）→ 釋出時跑這支 → 外部使用者 git pull / 抓 zip 取得更新。
#
# 用法（在 monorepo 任意位置執行皆可）：
#   ./gex-suite/publish-to-public.sh            # 一般發布（fast-forward）
#   ./gex-suite/publish-to-public.sh --force    # 第一次發布 / 覆蓋 public 舊 history
#
# 原理：git subtree split 把 gex-suite/ 攤平成獨立 history，push 到 public 的 main。
#   - subtree split 對相同 commit 會產生相同 SHA，所以第一次之後都是 fast-forward。
#   - 只發布「已 commit」的內容；gitignored 的 secret 不會被包含。

set -euo pipefail

PREFIX="gex-suite"
PUBLIC_URL="https://github.com/chunhua523/GEX_suite.git"
PUBLIC_REMOTE="gex-public"
SPLIT_BRANCH="_gex_publish_tmp"

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if [ ! -d "$PREFIX" ]; then
    echo "✗ 找不到 $PREFIX/ — 請在 Jeff-Project monorepo 內執行。"; exit 1
fi

# subtree 只發布「已 commit」內容；先擋住未 commit 的變更，避免發布舊狀態而不自知
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "⚠️  工作區有未 commit 的變更。subtree 只會發布『已 commit』的內容。"
    echo "    請先 commit（並 push monorepo）後再發布。"; exit 1
fi

# 確保 public remote 存在
if ! git remote get-url "$PUBLIC_REMOTE" &> /dev/null; then
    echo "→ 新增 remote $PUBLIC_REMOTE → $PUBLIC_URL"
    git remote add "$PUBLIC_REMOTE" "$PUBLIC_URL"
fi

echo "→ 從 $PREFIX/ 產生 subtree split..."
git branch -D "$SPLIT_BRANCH" 2>/dev/null || true
git subtree split --prefix="$PREFIX" -b "$SPLIT_BRANCH"

FORCE=""
[ "${1:-}" = "--force" ] && FORCE="--force"

echo "→ 推送到 $PUBLIC_REMOTE main${FORCE:+  [FORCE]}..."
if git push $FORCE "$PUBLIC_REMOTE" "$SPLIT_BRANCH:main"; then
    echo "✓ 發布完成。外部使用者下次 git pull / 抓 zip 即可取得更新。"
    STATUS=0
else
    echo
    echo "✗ push 被拒（多半是 public repo 仍有獨立版舊 history → non-fast-forward）。"
    echo "  第一次發布請改用： ./gex-suite/publish-to-public.sh --force"
    echo "  （會用 monorepo 的 gex-suite 內容覆蓋 public repo 舊 history）"
    STATUS=1
fi

git branch -D "$SPLIT_BRANCH" 2>/dev/null || true
exit $STATUS
