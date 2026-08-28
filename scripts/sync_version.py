"""把项目版本从 pyproject.toml 同步到其它会显示版本号的位置。

用法：
    uv run python scripts/sync_version.py

版本唯一来源是 pyproject.toml 的 [project].version。
脚本只做机械同步，不负责 bump 版本号 —— bump 请直接改 pyproject.toml。
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def sync_package_json(version: str) -> None:
    path = ROOT / "frontend" / "package.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") == version:
        print(f"  package.json 已是最新：{version}")
        return
    data["version"] = version
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  package.json -> {version}")


def sync_readme(version: str) -> None:
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^v\d+\.\d+(?:\.\d+)? 测试版", re.MULTILINE)
    new_text, count = pattern.subn(f"v{version} 测试版", text)
    if count == 0:
        print("  README.md 未找到 'vX.Y.Z 测试版' 行，跳过")
        return
    if new_text == text:
        print(f"  README.md 已是最新：v{version}")
        return
    path.write_text(new_text, encoding="utf-8")
    print(f"  README.md -> v{version} 测试版")


def main() -> int:
    version = read_pyproject_version()
    print(f"当前版本：{version}")
    sync_package_json(version)
    sync_readme(version)
    print("完成。uv.lock 由 `uv lock` / `uv sync` 自动更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
