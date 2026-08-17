"""
记忆类型注册表：内置 + 用户覆盖。

「高度自定义」的核心 —— 用户不改代码就能加记忆类型、改内置定义、关掉不要的。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.modules.memory import registry
from app.modules.memory.registry import BUILTIN_SCHEMAS_DIR, load_schemas
from app.modules.memory.schema import MemoryScopeKind, MergeOp

CUSTOM = """
memory_type: projects
scope: agent
description: 用户正在进行的项目。
directory: projects
filename_template: "{{ project_name }}.md"
fields:
  - name: project_name
    type: string
    merge_op: immutable
    description: 项目名。
  - name: content
    type: string
    merge_op: patch
    description: 项目概况。
"""


@pytest.fixture
def user_dir(tmp_path: Path) -> Iterator[Path]:
    d = tmp_path / "memory"
    d.mkdir(parents=True)
    registry.reset()
    yield d
    registry.reset()


def test_builtin_schemas_all_load() -> None:
    """
    内置定义必须全部合法。一个坏的会在启动时降级成"这个类型不存在"，
    而那个错误信息不指向 YAML。
    """
    result = load_schemas(user_dir=Path("does-not-exist"))

    assert result.diagnostics == []
    assert len(result.schemas) == 10
    assert {s.memory_type for s in result.by_scope(MemoryScopeKind.GLOBAL)} == {"profile"}
    assert {s.memory_type for s in result.by_scope(MemoryScopeKind.SESSION)} == {"events", "entities"}


def test_builtin_yaml_files_match_registered_types() -> None:
    """文件名与 memory_type 应一致，否则排错时对不上号。"""
    files = {p.stem for p in BUILTIN_SCHEMAS_DIR.glob("*.yaml")}
    registered = set(load_schemas(user_dir=Path("nope")).schemas)
    assert files == registered


def test_user_can_add_new_type(user_dir: Path) -> None:
    (user_dir / "projects.yaml").write_text(CUSTOM, encoding="utf-8")

    result = load_schemas(user_dir=user_dir)

    assert "projects" in result.schemas
    assert result.schemas["projects"].source.endswith("projects.yaml")
    # 内置的仍然在
    assert "profile" in result.schemas


def test_user_can_override_builtin(user_dir: Path) -> None:
    """
    同名整体覆盖。字段级合并会让"最终生效的定义是什么"难以推断。
    """
    (user_dir / "soul.yaml").write_text(
        """
memory_type: soul
scope: agent
description: 我改过的性格定义。
directory: ""
filename_template: soul.md
peer_enabled: false
fields:
  - name: content
    type: string
    merge_op: replace
    description: 整体重写而非打补丁。
""",
        encoding="utf-8",
    )

    result = load_schemas(user_dir=user_dir)

    soul = result.schemas["soul"]
    assert soul.fields[0].merge_op is MergeOp.REPLACE
    assert soul.source.endswith("soul.yaml")
    assert result.diagnostics == []


def test_user_can_disable_builtin(user_dir: Path) -> None:
    (user_dir / "trajectories.yaml").write_text(
        """
memory_type: trajectories
scope: agent
description: 关掉它。
directory: trajectories
filename_template: "{{ trajectory_name }}.md"
enabled: false
fields:
  - name: trajectory_name
    description: n
""",
        encoding="utf-8",
    )

    result = load_schemas(user_dir=user_dir)

    assert "trajectories" in result.schemas
    assert "trajectories" not in {s.memory_type for s in result.enabled()}


def test_one_bad_file_does_not_block_others(user_dir: Path) -> None:
    """一个坏 YAML 不该影响其它 —— 记 diagnostic 跳过它。"""
    (user_dir / "broken.yaml").write_text("memory_type: broken\nscope: nope\n", encoding="utf-8")
    (user_dir / "projects.yaml").write_text(CUSTOM, encoding="utf-8")

    result = load_schemas(user_dir=user_dir)

    assert "projects" in result.schemas
    assert "broken" not in result.schemas
    assert any("broken.yaml" in d.message for d in result.diagnostics)


def test_unparseable_yaml_is_reported(user_dir: Path) -> None:
    (user_dir / "bad.yaml").write_text("memory_type: [unclosed\n", encoding="utf-8")

    result = load_schemas(user_dir=user_dir)

    assert any("bad.yaml" in d.message for d in result.diagnostics)
    assert len(result.enabled()) == 10


def test_missing_builtin_dir_raises(tmp_path: Path) -> None:
    """
    内置一个都没加载 = 包装坏了。静默跑起来的话所有记忆写入都会以
    "未知记忆类型"失败，而那个错误不指向"schema 没加载"。
    """
    with pytest.raises(RuntimeError, match="记忆系统不可用"):
        load_schemas(builtin_dir=tmp_path / "nope", user_dir=tmp_path)


def test_reload_picks_up_changes(user_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """改了 config/memory/*.yaml 后不需要重启。"""
    monkeypatch.setattr(registry, "user_schemas_dir", lambda: user_dir)

    assert "projects" not in registry.get_schemas().schemas

    (user_dir / "projects.yaml").write_text(CUSTOM, encoding="utf-8")
    assert "projects" in registry.reload().schemas
