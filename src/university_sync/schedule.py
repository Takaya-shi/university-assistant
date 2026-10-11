"""毎日の定期実行（設計書 11.2）。macOS の launchd（ユーザーの LaunchAgent）から手動同期と同じ CLI を呼ぶ。

- 指定時刻に `run --if-stale` を起動する。ログイン時（Mac を止めていた後）にも一度起動し、
  直近に成功していなければ取りこぼしを取り戻す。
- 非対話なので、PATH・作業フォルダ・ログの出し先を明示する。認可が切れていたら待たずに終了し、同期状況に残す。
- launchd から起動したプロセスは macOS の保護（TCC）で ~/Documents を読めない。ソースは ~/Documents に置いたまま、
  実行用の環境を状態フォルダの runtime/ に作り、そこから起動する。コードを変えたら `schedule install` を再実行する。
"""

from __future__ import annotations

import getpass
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "local.university-sync"
PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
STALE_HOURS = 12


def _domain() -> str:
    return f"gui/{os.getuid()}"


def runtime_dir(cfg) -> Path:
    return cfg.state_dir / "runtime"


def build_runtime(cfg) -> Path:
    """ソースを ~/Documents の外へインストールし直す（editable ではなくコピー）。"""
    src = Path(__file__).resolve().parents[2]
    rt = runtime_dir(cfg)
    python = Path("/opt/homebrew/bin/python3") if Path("/opt/homebrew/bin/python3").exists() else Path(sys.executable)
    if not (rt / "bin" / "python").exists():
        subprocess.run([str(python), "-m", "venv", str(rt)], check=True)
    subprocess.run([str(rt / "bin" / "pip"), "install", "-q", "--disable-pip-version-check", "--upgrade",
                    f"{src}[google]"], check=True)
    return rt / "bin" / "university-sync"


def build_plist(cfg, hour: int, minute: int) -> dict:
    exe = runtime_dir(cfg) / "bin" / "university-sync"
    logs = cfg.state_dir / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(exe), "--config", str(cfg.path), "run", "--if-stale", str(STALE_HOURS)],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": True,
        "WorkingDirectory": str(cfg.state_dir),
        # USER/LOGNAME が無いと claude CLI がキーチェーンのログイン情報を見つけられない（2026-10-11 確認）
        "EnvironmentVariables": {"PATH": PATH, "HOME": str(Path.home()), "USER": getpass.getuser(),
                                 "LOGNAME": getpass.getuser(), "LANG": "ja_JP.UTF-8", "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(logs / "daily.log"),
        "StandardErrorPath": str(logs / "daily.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }


def install(cfg, time_str: str) -> str:
    hour, minute = (int(x) for x in time_str.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"時刻が不正: {time_str}")
    (cfg.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    build_runtime(cfg)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", _domain(), str(PLIST)], capture_output=True)
    with PLIST.open("wb") as f:
        plistlib.dump(build_plist(cfg, hour, minute), f)
    p = subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap に失敗: {p.stderr.strip()}")
    return str(PLIST)


def uninstall() -> bool:
    if not PLIST.exists():
        return False
    subprocess.run(["launchctl", "bootout", _domain(), str(PLIST)], capture_output=True)
    PLIST.unlink()
    return True


def status() -> str:
    if not PLIST.exists():
        return "未登録"
    with PLIST.open("rb") as f:
        p = plistlib.load(f)
    t = p["StartCalendarInterval"]
    loaded = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    state = "読み込み済み" if loaded.returncode == 0 else "未読み込み（停止中）"
    last = next((l.strip() for l in loaded.stdout.splitlines() if "last exit code" in l), "")
    return f"毎日 {t['Hour']:02d}:{t['Minute']:02d}（{state}）{(' ' + last) if last else ''}\n  設定: {PLIST}\n  ログ: {p['StandardOutPath']}"


def kickstart() -> None:
    subprocess.run(["launchctl", "kickstart", f"{_domain()}/{LABEL}"], check=True)
