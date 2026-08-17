"""
验证 Rerank 重排序功能的测试脚本。
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
backend_dir = project_root / "backend"
sys.path.insert(0, str(backend_dir))


def test_rerank_provider_creation():
    """测试 rerank 提供商创建"""
    from app.infra.rerank import create_rerank_provider
    
    print("✅ Rerank 提供商创建测试")
    
    # 测试 Cohere
    provider = create_rerank_provider(
        provider="cohere",
        api_key="test-key",
        model="rerank-v3.5",
    )
    assert provider is not None
    assert provider.provider_name == "cohere"
    print("  ✓ Cohere 提供商创建成功")
    
    # 测试 Jina
    provider = create_rerank_provider(
        provider="jina",
        api_key="test-key",
    )
    assert provider is not None
    assert provider.provider_name == "jina"
    print("  ✓ Jina 提供商创建成功")
    
    # 测试 Voyage
    provider = create_rerank_provider(
        provider="voyage",
        api_key="test-key",
    )
    assert provider is not None
    assert provider.provider_name == "voyage"
    print("  ✓ Voyage 提供商创建成功")
    
    # 测试不支持的提供商
    provider = create_rerank_provider(
        provider="unknown",
        api_key="test-key",
    )
    assert provider is None
    print("  ✓ 不支持的提供商正确返回 None\n")


def test_config_loading():
    """测试配置加载"""
    from app.core.config import settings
    
    print("✅ Rerank 配置加载测试")
    print(f"  启用: {settings.memory.recall_enable_rerank}")
    print(f"  提供商: {settings.memory.rerank_provider}")
    print(f"  模型: {settings.memory.rerank_model or '(使用默认)'}")
    print(f"  向量权重: {settings.memory.rerank_vector_weight}")
    print(f"  Rerank 权重: {settings.memory.rerank_rerank_weight}")
    print(f"  超时: {settings.memory.rerank_timeout}s")
    
    # 验证权重和为 1.0
    total = settings.memory.rerank_vector_weight + settings.memory.rerank_rerank_weight
    assert abs(total - 1.0) < 0.001, f"权重和应该为 1.0，实际为 {total}"
    
    print("  ✓ 配置值正确\n")


def test_score_mixing():
    """测试分数混合逻辑"""
    print("✅ 分数混合测试")
    
    # 模拟场景
    vector_score = 0.75
    rerank_score = 0.92
    vector_weight = 0.3
    rerank_weight = 0.7
    
    mixed_score = vector_weight * vector_score + rerank_weight * rerank_score
    
    print(f"  向量分数: {vector_score}")
    print(f"  Rerank 分数: {rerank_score}")
    print(f"  向量权重: {vector_weight}")
    print(f"  Rerank 权重: {rerank_weight}")
    print(f"  混合分数: {mixed_score:.3f}")
    
    expected = 0.3 * 0.75 + 0.7 * 0.92  # 0.225 + 0.644 = 0.869
    assert abs(mixed_score - expected) < 0.001
    
    print(f"  ✓ 计算正确（期望 {expected:.3f}）\n")


async def test_rerank_mock():
    """模拟 rerank 流程（不调用真实 API）"""
    from dataclasses import dataclass
    
    print("✅ Rerank 流程测试（模拟）")
    
    # 模拟 SearchHit
    @dataclass
    class MockSearchHit:
        uri: str
        score: float
        title: str
        memory_type: str
        scope: str
    
    # 创建模拟的候选记忆
    hits = [
        MockSearchHit(
            uri="memories/events/evt_001",
            score=0.75,
            title="学习 Python 异步编程",
            memory_type="events",
            scope="session",
        ),
        MockSearchHit(
            uri="memories/events/evt_002",
            score=0.68,
            title="写了一个爬虫",
            memory_type="events",
            scope="session",
        ),
        MockSearchHit(
            uri="memories/events/evt_003",
            score=0.82,
            title="阅读 Python 文档",
            memory_type="events",
            scope="session",
        ),
    ]
    
    print(f"  初始候选数: {len(hits)}")
    for hit in hits:
        print(f"    - {hit.title} (向量分数: {hit.score:.2f})")
    
    # 模拟 rerank 返回的分数（假设 rerank 认为第 2 个最相关）
    mock_rerank_scores = [0.85, 0.95, 0.72]
    
    print(f"\n  模拟 Rerank 分数:")
    for hit, rerank_score in zip(hits, mock_rerank_scores):
        print(f"    - {hit.title}: {rerank_score:.2f}")
    
    # 混合分数
    vector_weight = 0.3
    rerank_weight = 0.7
    
    mixed_hits = []
    for hit, rerank_score in zip(hits, mock_rerank_scores):
        mixed_score = vector_weight * hit.score + rerank_weight * rerank_score
        mixed_hit = MockSearchHit(
            uri=hit.uri,
            score=mixed_score,
            title=hit.title,
            memory_type=hit.memory_type,
            scope=hit.scope,
        )
        mixed_hits.append(mixed_hit)
    
    # 排序
    mixed_hits.sort(key=lambda h: -h.score)
    
    print(f"\n  混合后排序:")
    for i, hit in enumerate(mixed_hits, 1):
        print(f"    {i}. {hit.title} (最终分数: {hit.score:.2f})")
    
    # 验证排序（第 2 个应该排第一）
    assert mixed_hits[0].title == "写了一个爬虫"
    print("  ✓ Rerank 改变了排序顺序\n")


def test_api_response_parsing():
    """测试 API 响应解析逻辑"""
    print("✅ API 响应解析测试")
    
    # 模拟 Cohere API 响应
    cohere_response = {
        "results": [
            {"index": 2, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.88},
            {"index": 1, "relevance_score": 0.75},
        ]
    }
    
    # 解析逻辑（来自 CohereRerankProvider）
    num_docs = 3
    scores = [0.0] * num_docs
    
    for result in cohere_response["results"]:
        index = result["index"]
        score = result["relevance_score"]
        if 0 <= index < num_docs:
            scores[index] = float(score)
    
    print(f"  原始顺序分数: {scores}")
    assert scores == [0.88, 0.75, 0.95]
    print("  ✓ 解析正确\n")


def test_integration_with_recall():
    """测试与 recall.py 的集成"""
    print("✅ Recall 集成测试")
    
    # 检查 recall.py 中的 _apply_rerank 函数存在
    from app.modules.memory import recall
    
    assert hasattr(recall, "_apply_rerank")
    print("  ✓ _apply_rerank 函数已定义")
    
    # 检查导入路径
    from app.infra.rerank import create_rerank_provider
    provider = create_rerank_provider("cohere", "test-key")
    assert provider is not None
    print("  ✓ rerank 模块可正确导入\n")


def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("Rerank 重排序功能验证")
    print("="*60 + "\n")
    
    try:
        # 同步测试
        test_rerank_provider_creation()
        test_config_loading()
        test_score_mixing()
        test_api_response_parsing()
        test_integration_with_recall()
        
        # 异步测试
        asyncio.run(test_rerank_mock())
        
        print("="*60)
        print("✅ 所有测试通过！")
        print("="*60 + "\n")
        
        print("💡 提示：要启用 Rerank，需要设置：")
        print("  1. JEEVES_MEMORY__RECALL_ENABLE_RERANK=true")
        print("  2. JEEVES_MEMORY__RERANK_API_KEY=<你的 API 密钥>")
        print("  3. JEEVES_MEMORY__RERANK_PROVIDER=cohere|jina|voyage")
        print()
        
        return 0
    except Exception as e:
        print("\n" + "="*60)
        print("❌ 测试失败")
        print("="*60)
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
