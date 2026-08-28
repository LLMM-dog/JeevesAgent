"""项目版本统一入口。

版本以 pyproject.toml 为唯一来源。优先读取已安装包的元数据，
拿不到时（例如直接从源码跑、包未安装）回落到解析 pyproject.toml。
"""

import tomllib
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def get_version() -> str:
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("jeeves")
    except Exception:  # noqa: BLE001 —— 源码直跑时包可能未安装
        pass

    try:
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except Exception:  # noqa: BLE001
        return "0.0.0"
