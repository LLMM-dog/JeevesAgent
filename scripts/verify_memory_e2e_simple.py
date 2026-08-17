"""
简化版端到端记忆循环验证。

避免并发问题，专注验证核心流程。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from dotenv import load_dotenv

env_verify = PROJECT_ROOT / ".env.verify"
if not env_verify.exists():
    print(f"❌ {env_verify} 不存在")
    sys.exit(1)

load_dotenv(env_verify)
os.environ["JEEVES_SECURITY__ENCRYPTION_KEY"] = "xr9i7PEx3aHu2bTacB0tEG1VnCmkvlHJu2fZ-7XQDWs="

import structlog
import logging

# 设置为 DEBUG 级别查看详细日志
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.infra.llm.port import ResolvedModel
from app.modules.memory import service as memory_service
from app.modules.memory.layout import MemoryScope
from app.modules.memory.recall import recall_memories
from app.modules.memory import vectorize as vec_mod

INFO = "ℹ️ "
OK = "✅"
FAIL = "❌"

AGENT_ID = "test_agent"


async def main():
    print("\n" + "=" * 80)
    print("简化版端到端记忆循环验证")
    print("=" * 80)
    
    # 创建测试数据库
    db_path = PROJECT_ROOT / "data" / "test_e2e_simple.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    
    # 清空记忆目录（使用配置中的路径）
    memory_root = settings.memory_dir
    print(f"\n{INFO} 记忆目录: {memory_root}")
    
    if memory_root.exists():
        import shutil
        shutil.rmtree(memory_root)
        print(f"{INFO} 已清空记忆目录")
    memory_root.mkdir(parents=True, exist_ok=True)
    
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    
    from app.infra.db.base import Base
    # 导入所有模型以注册表
    import app.modules.session.models  # noqa
    import app.modules.memory.models  # noqa
    import app.modules.agent.models  # noqa
    import app.modules.endpoint.models  # noqa
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    Session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with Session_maker() as db:
        # 1. 配置嵌入模型
        print(f"\n{INFO} 1. 配置嵌入模型")
        embed_base_url = os.getenv("VERIFY_EMBEDDING_BASE_URL", os.getenv("VERIFY_BASE_URL"))
        embed_api_key = os.getenv("VERIFY_EMBEDDING_API_KEY", os.getenv("VERIFY_API_KEY"))
        embed_model = os.getenv("VERIFY_EMBEDDING_MODEL", "text-embedding-3-small")
        
        if not embed_api_key:
            print(f"{FAIL} 未配置嵌入模型凭证")
            return
        
        embedding_model = ResolvedModel(
            model_id=embed_model,
            base_url=embed_base_url or "",
            api_key=embed_api_key,
            context_window=8192,
        )
        
        print(f"{OK} 嵌入模型: {embed_model}")
        
        # 2. 初始化智能体
        print(f"\n{INFO} 2. 初始化智能体")
        await memory_service.init_agent(AGENT_ID, db=db)
        print(f"{OK} 智能体: {AGENT_ID}")
        
        # 3. 手动写入测试记忆
        print(f"\n{INFO} 3. 写入测试记忆")
        scope = MemoryScope(agent_id=AGENT_ID)
        
        memories = [
            ("Python 开发", "- 喜欢使用 Python 编程\n- 偏好 FastAPI 框架"),
            ("测试工具", "- 使用 pytest 做单元测试\n- 测试覆盖率要达到 80%"),
            ("代码质量", "- 使用 ruff 做代码检查\n- 使用 mypy 做类型检查"),
        ]
        
        uris = []
        for topic, content in memories:
            try:
                result = await memory_service.write(
                    scope=scope,
                    memory_type="preferences",
                    fields={"topic": topic, "content": content},
                    db=db,
                )
                uris.append(result.uri)
                print(f"  - 写入: {topic}")
                print(f"    URI: {result.uri}, created={result.created}, changed={result.changed}")
            except Exception as e:
                print(f"  {FAIL} 写入失败: {topic}, error={e}")
                import traceback
                traceback.print_exc()
        
        await db.commit()
        print(f"{OK} 写入了 {len(memories)} 条偏好记忆")
        
        # 4. 向量化
        print(f"\n{INFO} 4. 向量化记忆")
        
        async def read_item(uri: str):
            return await memory_service.read_uri(uri)
        
        vec_report = await vec_mod.vectorize_uris(
            db=db,
            uris=uris,
            model=embedding_model,
            read_item=read_item,
        )
        
        # 提交向量索引到数据库
        await db.commit()
        
        print(f"{OK} 向量化: {vec_report.succeeded}/{vec_report.attempted} 成功")
        
        # 调试：检查数据库中的索引行
        from sqlalchemy import select
        from app.modules.memory.models_db import MemoryIndex
        
        result = await db.execute(select(MemoryIndex))
        all_indexes = list(result.scalars())
        
        print(f"\n{INFO} 数据库中的索引行: {len(all_indexes)} 条")
        for idx in all_indexes[:5]:
            print(f"  - {idx.uri}")
            print(f"    scope={idx.scope}, agent_id={idx.agent_id}, session_id={idx.session_id}")
            print(f"    memory_type={idx.memory_type}, has_embedding={idx.embedding is not None}")
            if idx.embedding:
                print(f"    model={idx.embedding_model}, dim={idx.embedding_dim}")
        
        if vec_report.errors:
            print(f"{FAIL} 向量化错误:")
            for err in vec_report.errors:
                print(f"  - {err}")
        
        # 5. 测试召回
        print(f"\n{INFO} 5. 测试召回")
        
        test_queries = [
            "我该用什么测试框架？",
            "如何做代码检查？",
            "Python Web 开发用什么框架？",
        ]
        
        all_passed = True
        
        for query in test_queries:
            print(f"\n  查询: {query}")
            
            recall_result = await recall_memories(
                db=db,
                session_id="test_session",
                query=query,
                agent_id=AGENT_ID,
                embedding_model=embedding_model,
            )
            
            if recall_result.memories:
                print(f"  {OK} 召回了 {len(recall_result.memories)} 条记忆:")
                for memory_type, item in recall_result.memories[:3]:
                    print(f"    - [{memory_type}] {item.title}")
                    print(f"      {item.body[:100]}...")
            else:
                print(f"  {FAIL} 没有召回任何记忆")
                all_passed = False
        
        # 6. 验证多智能体隔离
        print(f"\n{INFO} 6. 验证多智能体隔离")
        
        # 创建另一个智能体
        AGENT_B = "test_agent_b"
        await memory_service.init_agent(AGENT_B, db=db)
        scope_b = MemoryScope(agent_id=AGENT_B)
        
        # 给 B 写入不同的记忆
        await memory_service.write(
            scope=scope_b,
            memory_type="preferences",
            fields={"topic": "Java 开发", "content": "- 喜欢使用 Java 和 Spring Boot"},
            db=db,
        )
        await db.commit()
        
        # A 召回，不应该看到 B 的记忆
        recall_a = await recall_memories(
            db=db,
            session_id="test_session",
            query="Java Spring",
            agent_id=AGENT_ID,
            embedding_model=embedding_model,
        )
        
        has_java = any("Java" in item.body for _, item in recall_a.memories)
        
        if not has_java:
            print(f"  {OK} 智能体 A 看不到智能体 B 的记忆（隔离正确）")
        else:
            print(f"  {FAIL} 智能体 A 看到了智能体 B 的记忆（隔离失败）")
            all_passed = False
        
        # 7. 总结
        print(f"\n{'=' * 80}")
        if all_passed and vec_report.succeeded == len(memories):
            print(f"{OK} 所有测试通过！")
        else:
            print(f"{FAIL} 部分测试失败")
        print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
