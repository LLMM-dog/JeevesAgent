"""
端到端记忆循环验证。

验证完整流程：
1. 用户对话
2. 自动触发记忆提取
3. LLM 提取记忆
4. 向量化存储
5. 下次对话时召回记忆
6. 基于记忆回答

用法：
    uv run python scripts/verify_memory_e2e.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from dotenv import load_dotenv

# 加载 .env.verify
env_verify = PROJECT_ROOT / ".env.verify"
if not env_verify.exists():
    print(f"❌ {env_verify} 不存在，请复制 .env.verify.example 并填写凭证")
    sys.exit(1)

load_dotenv(env_verify)

# 设置加密密钥（测试用）
os.environ["JEEVES_SECURITY__ENCRYPTION_KEY"] = "xr9i7PEx3aHu2bTacB0tEG1VnCmkvlHJu2fZ-7XQDWs="

import structlog
import logging

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.modules.agent.messages import Msg
from app.infra.llm.port import ResolvedModel
from app.modules.memory import service as memory_service
from app.modules.memory.auto_commit import check_and_trigger
from app.modules.memory.layout import MemoryScope
from app.modules.memory.recall import recall_memories
from app.modules.session import repo as session_repo
from app.modules.session.models import Session

INFO = "ℹ️ "
OK = "✅"
FAIL = "❌"

AGENT_ID = "agent_e2e_test"


async def main():
    print("\n" + "=" * 80)
    print("端到端记忆循环验证")
    print("=" * 80)
    
    # 创建测试数据库
    db_path = PROJECT_ROOT / "data" / "test_e2e.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    
    # 创建表
    from app.infra.db.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    Session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    # === 第一部分：提取和向量化 ===
    async with Session_maker() as db:
        # 1. 设置嵌入模型
        print(f"\n{INFO} 1. 配置嵌入模型")
        embed_base_url = os.getenv("VERIFY_EMBEDDING_BASE_URL", os.getenv("VERIFY_BASE_URL"))
        embed_api_key = os.getenv("VERIFY_EMBEDDING_API_KEY", os.getenv("VERIFY_API_KEY"))
        embed_model = os.getenv("VERIFY_EMBEDDING_MODEL", "text-embedding-3-small")
        
        if not embed_api_key:
            print(f"{FAIL} 未配置嵌入模型凭证，请在 .env.verify 中设置")
            return
        
        # 创建嵌入模型 mock
        embedding_model = ResolvedModel(
            model_id=embed_model,
            base_url=embed_base_url or "",
            api_key=embed_api_key,
            context_window=8192,
            endpoint_name="test_embedding",
            purpose="embedding",
        )
        # 添加嵌入维度到 extra（某些代码可能需要）
        embedding_model.extra = {"embedding_dim": 1536}  # type: ignore
        
        print(f"{OK} 嵌入模型: {embed_model}")
        
        # 2. 创建会话和智能体
        print(f"\n{INFO} 2. 创建测试会话")
        ws = await session_repo.ensure_default_workspace(db, str(PROJECT_ROOT / "workspace"))
        
        session = Session(
            id="ses_e2e_test",
            title="记忆循环测试",
            workspace_id=ws.id,
        )
        session.set_agent_ids([AGENT_ID])
        db.add(session)
        await db.commit()
        
        # 初始化智能体记忆
        await memory_service.init_agent(AGENT_ID, db=db)
        
        print(f"{OK} 会话: {session.id}")
        print(f"{OK} 智能体: {AGENT_ID}")
        
        # 3. 模拟第一轮对话（提供信息）
        print(f"\n{INFO} 3. 第一轮对话：用户告知偏好")
        
        # 添加足够多的消息，确保能触发提取（keep_recent=3 会保留最近 3 轮）
        messages_round1 = [
            ("user", "你好，我想告诉你我的编程偏好"),
            ("assistant", "好的，请告诉我"),
            ("user", "我喜欢使用 Python 编程，特别是 FastAPI 框架"),
            ("assistant", "好的，我记住了你喜欢 Python 和 FastAPI"),
            ("user", "我还喜欢用 pytest 做单元测试"),
            ("assistant", "明白了，你偏好使用 pytest 进行测试"),
            ("user", "代码风格方面，我喜欢用 ruff 做 lint"),
            ("assistant", "好的，记住了用 ruff 做代码检查"),
            ("user", "数据库我偏好用 PostgreSQL"),
            ("assistant", "明白，你偏好 PostgreSQL 数据库"),
        ]
        
        for role, content in messages_round1:
            await session_repo.append_message(
                db, session.id, Msg(role=role, content=content, agent_name=AGENT_ID)
            )
        
        print(f"{OK} 添加了 {len(messages_round1)} 条消息")
        
        # 4. 模拟 LLM 提取
        print(f"\n{INFO} 4. 手动触发记忆提取")
        
        # 创建假 LLM 返回（模拟提取结果）
        async def fake_llm_extract(messages, tools=None):
            return json.dumps({
                "operations": [
                    {
                        "memory_type": "preferences",
                        "page_id": None,
                        "topic": "Python 和 FastAPI",
                        "content": "- 喜欢使用 Python 编程语言\n- 特别偏好 FastAPI 框架进行 Web 开发"
                    },
                    {
                        "memory_type": "preferences",
                        "page_id": None,
                        "topic": "测试",
                        "content": "- 使用 pytest 框架进行单元测试"
                    },
                    {
                        "memory_type": "preferences",
                        "page_id": None,
                        "topic": "代码质量",
                        "content": "- 使用 ruff 做代码 lint 检查"
                    }
                ],
                "delete_page_ids": []
            })
        
        from app.modules.memory.commit import commit_session
        
        extract_report = await commit_session(
            db, session_id=session.id, llm_call=fake_llm_extract
        )
        
        print(f"{OK} 提取完成: {extract_report.summary()}")
        
        # 5. 向量化记忆
        print(f"\n{INFO} 5. 向量化记忆")
        
        from app.modules.memory import vectorize as vec_mod
        
        scope = MemoryScope(agent_id=AGENT_ID, session_id=session.id)
        
        # 列出所有记忆
        from app.modules.memory import registry
        schemas = registry.get_schemas()
        pref_schema = schemas.get("preferences")
        
        if pref_schema:
            items = await memory_service._store.list_items(scope, pref_schema)
            print(f"{INFO} 找到 {len(items)} 条偏好记忆")
            
            # 向量化
            uris = [item.uri for item in items]
            
            async def read_item(uri: str):
                return await memory_service.get_by_uri(uri)
            
            vec_report = await vec_mod.vectorize_uris(
                db=db,
                uris=uris,
                model=embedding_model,
                read_item=read_item,
            )
            
            print(f"{OK} 向量化: {vec_report.succeeded}/{vec_report.attempted} 成功")
    
    # === 第二部分：召回（使用新的 session） ===
    async with Session_maker() as db2:
        # 6. 第二轮对话（触发召回）
        print(f"\n{INFO} 6. 第二轮对话：查询相关信息")
    # === 第二部分：召回（使用新的 session） ===
    async with Session_maker() as db2:
        # 6. 第二轮对话（触发召回）
        print(f"\n{INFO} 6. 第二轮对话：查询相关信息")
        
        user_query = "我该用什么测试框架？"
        
        await session_repo.append_message(
            db2, session.id, Msg(role="user", content=user_query, agent_name=AGENT_ID)
        )
        
        print(f"{INFO} 用户查询: {user_query}")
        
        # 7. 召回记忆
        print(f"\n{INFO} 7. 召回相关记忆")
        
        recall_result = await recall_memories(
            db=db2,
            session_id=session.id,
            query=user_query,
            agent_id=AGENT_ID,
            embedding_model=embedding_model,
        )
        
        print(f"{OK} 召回了 {len(recall_result.memories)} 条记忆")
        print(f"{INFO} 字符数: {recall_result.total_chars}")
        
        if recall_result.memories:
            print(f"\n{INFO} 召回的记忆:")
            for memory_type, hit in recall_result.memories:
                print(f"  - [{memory_type}] {hit.title} (分数: {hit.score:.3f})")
                print(f"    {hit.content[:100]}...")
        
        # 8. 验证召回内容
        print(f"\n{INFO} 8. 验证召回结果")
        
        checks = []
        
        # 检查是否召回了 pytest 相关的记忆
        pytest_mentioned = any(
            "pytest" in hit.content.lower()
            for _, hit in recall_result.memories
        )
        checks.append((pytest_mentioned, "召回了 pytest 相关记忆"))
        
        # 检查是否有偏好类型
        has_preference = any(
            memory_type == "preferences"
            for memory_type, _ in recall_result.memories
        )
        checks.append((has_preference, "召回了偏好类型记忆"))
        
        # 检查渲染是否正确
        has_rendered = bool(recall_result.rendered)
        checks.append((has_rendered, "记忆已渲染成文本"))
        
        # 打印检查结果
        print(f"\n{INFO} 验证结果:")
        for passed, desc in checks:
            status = OK if passed else FAIL
            print(f"  {status} {desc}")
        
        # 9. 显示最终的系统提示词（模拟）
        if recall_result.rendered:
            print(f"\n{INFO} 9. 注入到系统提示词的内容:")
            print("-" * 80)
            print(recall_result.rendered[:500])
            if len(recall_result.rendered) > 500:
                print(f"... (共 {len(recall_result.rendered)} 字符)")
            print("-" * 80)
        
        # 10. 总结
        print(f"\n{'=' * 80}")
        all_passed = all(passed for passed, _ in checks)
        if all_passed:
            print(f"{OK} 端到端记忆循环验证通过！")
        else:
            print(f"{FAIL} 部分检查失败，请查看上面的输出")
        print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
