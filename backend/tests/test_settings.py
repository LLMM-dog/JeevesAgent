"""
用户可调设置。

## 为什么需要这层

core/config.py 的值来自环境变量，改一次要重启。而记忆的超参数
属于「用户按自己的模型调」的东西 —— 换个小窗口模型就要调截断，
那不该需要改 .env 再重启。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.config import MemoryConfig, settings
from app.modules.settings import service as svc
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def restore_settings() -> Iterator[None]:
    """
    每个测试后把 settings.memory 恢复默认。

    apply() 改的是【进程内的全局对象】，不恢复会让测试之间互相污染 ——
    而那种污染的表现是"单独跑通过，一起跑失败"，最难查。
    """
    before = settings.memory.model_dump()
    yield
    for key, value in before.items():
        setattr(settings.memory, key, value)


def test_whitelist_covers_the_tunable_hyperparameters() -> None:
    """
    白名单要覆盖用户真正需要调的项。

    ## 为什么必须有白名单

    不能让前端写任意 key —— security.encryption_key 或 db.path
    被改掉会直接破坏系统。
    """
    keys = set(svc.SETTABLE)
    assert "memory.keep_recent_turns" in keys
    assert "memory.eager_prefetch" in keys
    assert "memory.max_msg_chars" in keys
    assert "memory.search_min_score" in keys
    # 危险项绝不能在里面 —— 指【内部】密钥（加密密钥）和数据库路径。
    # 用户自己填的第三方服务 Key（如 websearch.tavily_api_key）是有意开放
    # 给用户配的，不属于这里的"危险项"。
    assert not any(k.startswith("db.") or "encryption" in k for k in keys)


def test_every_whitelisted_key_exists_in_config() -> None:
    """
    白名单里的 key 必须真的存在于 settings 上。

    写错一个字母的话 apply() 会静默跳过，用户在前端改了却没效果 ——
    而那个"没效果"没有任何报错。
    """
    for key in svc.SETTABLE:
        node = settings
        for part in key.split("."):
            assert hasattr(node, part), f"{key} 在 settings 里不存在"
            node = getattr(node, part)


def test_describe_exposes_type_and_range_for_frontend() -> None:
    """
    前端不该硬编码可调项列表 —— 那份列表会和后端不同步。
    类型、范围、说明都从后端来。
    """
    items = {i["key"]: i for i in svc.describe()}

    turns = items["memory.keep_recent_turns"]
    assert turns["type"] == "int"
    assert turns["min"] == 0 and turns["max"] == 50
    assert turns["label"]
    assert turns["value"] == settings.memory.keep_recent_turns

    assert items["memory.eager_prefetch"]["type"] == "bool"
    assert items["memory.search_min_score"]["type"] == "float"


def test_bool_strings_are_parsed_not_truthy_cast() -> None:
    """
    bool("false") 是 True —— 直接转会让"关闭"变成"开启"。
    这是个真实会踩的坑：前端传 JSON 时布尔可能变成字符串。
    """
    assert svc.validate("memory.eager_prefetch", "false") is False
    assert svc.validate("memory.eager_prefetch", "0") is False
    assert svc.validate("memory.eager_prefetch", "off") is False
    assert svc.validate("memory.eager_prefetch", "true") is True
    assert svc.validate("memory.eager_prefetch", True) is True

    with pytest.raises(ValueError, match="布尔"):
        svc.validate("memory.eager_prefetch", "maybe")


def test_out_of_range_values_are_rejected() -> None:
    """
    范围校验是必须的：max_msg_chars=0 会让截断把所有消息切成空串，
    而后果要到"提取产出 0 条记忆"时才显现，根因完全看不出来。
    """
    with pytest.raises(ValueError, match="不能小于"):
        svc.validate("memory.max_msg_chars", 10)
    with pytest.raises(ValueError, match="不能大于"):
        svc.validate("memory.max_msg_chars", 999_999)
    with pytest.raises(ValueError, match="不允许修改"):
        svc.validate("security.encryption_key", "x")


def test_apply_changes_live_settings() -> None:
    svc.apply({"memory.keep_recent_turns": 7, "memory.eager_prefetch": False})

    assert settings.memory.keep_recent_turns == 7
    assert settings.memory.eager_prefetch is False


def test_apply_ignores_unknown_paths() -> None:
    """
    不创建新属性 —— 那会掩盖拼写错误（写错的 key 会静默变成
    一个没人读的新字段）。
    """
    svc.apply({"memory.does_not_exist": 1, "nope.at.all": 2})
    assert not hasattr(settings.memory, "does_not_exist")


@pytest.mark.asyncio
async def test_set_many_persists_and_takes_effect(db: AsyncSession) -> None:
    await svc.set_many(db, {"memory.keep_recent_turns": 5, "memory.max_msg_chars": 2000})

    assert settings.memory.keep_recent_turns == 5
    assert settings.memory.max_msg_chars == 2000

    # 落库了 —— 重启后仍然生效
    stored = await svc.load_all(db)
    assert stored["memory.keep_recent_turns"] == 5
    assert stored["memory.max_msg_chars"] == 2000


@pytest.mark.asyncio
async def test_set_many_is_all_or_nothing(db: AsyncSession) -> None:
    """
    部分生效会让用户看到混合状态（改了 3 项、2 项生效），
    而他不知道是哪 2 项。
    """
    original = settings.memory.keep_recent_turns

    with pytest.raises(ValueError):
        await svc.set_many(
            db,
            {
                "memory.keep_recent_turns": 8,  # 合法
                "memory.max_msg_chars": -1,  # 非法
            },
        )

    assert settings.memory.keep_recent_turns == original, "整批失败时合法项也不该生效"
    assert await svc.load_all(db) == {}


@pytest.mark.asyncio
async def test_reload_applies_stored_values(db: AsyncSession) -> None:
    """启动时要能把库里的设置应用上。"""
    await svc.set_many(db, {"memory.prefetch_topn": 12})
    # 模拟重启：把内存值改回默认
    settings.memory.prefetch_topn = MemoryConfig().prefetch_topn

    await svc.reload(db)

    assert settings.memory.prefetch_topn == 12


@pytest.mark.asyncio
async def test_reset_restores_defaults(db: AsyncSession) -> None:
    """
    恢复默认 = 删行 + 把内存值改回代码默认。

    只删行不够 —— settings 对象已经被 apply 覆盖过，
    删行不会让它自动回到默认值。
    """
    defaults = MemoryConfig()
    await svc.set_many(db, {"memory.keep_recent_turns": 9, "memory.tool_search_limit": 20})

    removed = await svc.reset(db)

    assert removed == 2
    assert settings.memory.keep_recent_turns == defaults.keep_recent_turns
    assert settings.memory.tool_search_limit == defaults.tool_search_limit
    assert await svc.load_all(db) == {}


@pytest.mark.asyncio
async def test_reset_can_target_specific_keys(db: AsyncSession) -> None:
    defaults = MemoryConfig()
    await svc.set_many(db, {"memory.keep_recent_turns": 9, "memory.tool_search_limit": 20})

    await svc.reset(db, ["memory.keep_recent_turns"])

    assert settings.memory.keep_recent_turns == defaults.keep_recent_turns
    assert settings.memory.tool_search_limit == 20, "没指定的项不该被恢复"


@pytest.mark.asyncio
async def test_stale_keys_do_not_break_loading(db: AsyncSession) -> None:
    """
    白名单收缩后残留的旧 key 要被忽略 —— 用户不该因为我们删了
    一个设置项就打不开设置页。
    """
    from app.modules.settings.models import AppSetting

    db.add(AppSetting(key="memory.removed_long_ago", value="1"))
    db.add(AppSetting(key="memory.keep_recent_turns", value="4"))
    await db.commit()

    values = await svc.load_all(db)

    assert values == {"memory.keep_recent_turns": 4}


@pytest.mark.asyncio
async def test_corrupt_value_does_not_break_loading(db: AsyncSession) -> None:
    """存了非法值（手工改库、或类型改过）时忽略那一项而不是整体失败。"""
    from app.modules.settings.models import AppSetting

    db.add(AppSetting(key="memory.keep_recent_turns", value="not_a_number"))
    await db.commit()

    assert await svc.load_all(db) == {}


@pytest.mark.asyncio
async def test_settings_actually_affect_extraction(db: AsyncSession) -> None:
    """
    【端到端】改了设置要真的影响行为，而不只是存下来。

    这条测的是"配置有没有被真正消费"—— 我之前有过
    embedding_template 声明了但零调用的先例。
    """
    from app.modules.agent.messages import Msg
    from app.modules.memory.extract_input import prepare

    msgs = [Msg(role="user", content=f"第{i}轮") for i in range(6)]

    await svc.set_many(db, {"memory.keep_recent_turns": 1})
    kept_one = prepare(msgs, [1] * 6)

    await svc.set_many(db, {"memory.keep_recent_turns": 4})
    kept_four = prepare(msgs, [1] * 6)

    assert len(kept_one.messages) > len(kept_four.messages), "改设置必须真的改变截断行为"
    assert kept_one.held_back_turns == 1
    assert kept_four.held_back_turns == 4
