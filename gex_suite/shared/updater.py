"""Check for newer releases by comparing ``pyproject.toml`` on GitHub Raw."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from gex_suite import __version__ as LOCAL_VERSION
from gex_suite.shared.paths import PROJECT_ROOT


def _parse_version_from_pyproject(text: str) -> str | None:
    m = re.search(r'version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def fetch_raw_text(url: str, *, timeout: int = 20) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "GEX-Suite-update-check"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted URL builder)
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")
    except (URLError, HTTPError, TimeoutError, OSError):
        return None


@dataclass
class UpdateCheckResult:
    local_version: str
    remote_version: str | None
    is_up_to_date: bool | None
    """None if check failed (see ``error_message``)."""
    error_message: str
    remote_url: str = ""


def check_pyproject_on_github(
    *,
    user: str,
    repo: str,
    branch: str,
    remote_pyproject_path: str,
) -> UpdateCheckResult:
    user = (user or "").strip()
    repo = (repo or "").strip()
    branch = (branch or "main").strip()
    path = (remote_pyproject_path or "pyproject.toml").strip().lstrip("/")
    if not user or not repo:
        return UpdateCheckResult(
            LOCAL_VERSION,
            None,
            None,
            "請在 data/suite_config.json 設定 update_github_user 與 update_github_repo。",
        )
    url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
    text = fetch_raw_text(url)
    if text is None:
        return UpdateCheckResult(
            LOCAL_VERSION,
            None,
            None,
            "無法連線或下載遠端 pyproject.toml（請確認網路、儲存庫名稱與分支）。",
            remote_url=url,
        )
    remote_ver = _parse_version_from_pyproject(text)
    if not remote_ver:
        return UpdateCheckResult(
            LOCAL_VERSION,
            None,
            None,
            "遠端檔案中找不到 version = \"…\" 欄位。",
            remote_url=url,
        )
    try:
        from packaging.version import Version

        is_newer = Version(remote_ver) > Version(LOCAL_VERSION)
        is_up = not is_newer
    except Exception:
        is_up = remote_ver == LOCAL_VERSION
    return UpdateCheckResult(LOCAL_VERSION, remote_ver, is_up, "", remote_url=url)


def run_git_pull_ff_only(project_root: Path | None = None) -> tuple[bool, str]:
    root = project_root or PROJECT_ROOT
    if not (root / ".git").is_dir():
        return False, "安裝目錄沒有 .git（例如 pip / exe 安裝），無法自動 git pull。請改用手動更新。"
    try:
        proc = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        msg = "\n".join(x for x in (out, err) if x)
        if proc.returncode == 0:
            return True, msg or "git pull 已完成。"
        return False, msg or f"git pull 失敗（結束碼 {proc.returncode}）。"
    except FileNotFoundError:
        return False, "找不到 git 指令，請安裝 Git 並加入 PATH。"
    except subprocess.TimeoutExpired:
        return False, "git pull 逾時。"
    except Exception as exc:
        return False, str(exc)


class GitHubVersionCheckThread(QThread):
    finished = Signal(object)

    def __init__(
        self,
        *,
        user: str,
        repo: str,
        branch: str,
        remote_pyproject_path: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._user = user
        self._repo = repo
        self._branch = branch
        self._path = remote_pyproject_path

    def run(self) -> None:
        res = check_pyproject_on_github(
            user=self._user,
            repo=self._repo,
            branch=self._branch,
            remote_pyproject_path=self._path,
        )
        self.finished.emit(res)


class GitPullThread(QThread):
    finished = Signal(bool, str)

    def __init__(self, project_root: Path | None = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._root = project_root

    def run(self) -> None:
        ok, msg = run_git_pull_ff_only(self._root)
        self.finished.emit(ok, msg)
