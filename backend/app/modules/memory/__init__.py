"""
记忆系统。

对外只用 service 模块 —— 其它文件都是它的实现细节。

    from app.modules.memory import service as memory
    from app.modules.memory.models import MemoryScope

    scope = MemoryScope(agent_id="adf_xxx", session_id="ses_yyy")
    await memory.write(scope, "preferences", {"topic": "testing", "content": "..."}, db=db)

设计见 docs/architecture/memory.md，记忆类型的 YAML 格式见
docs/architecture/memory-schema.md。
"""
