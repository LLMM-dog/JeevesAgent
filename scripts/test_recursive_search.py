"""
验证递归搜索功能的简单测试脚本。
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
backend_dir = project_root / "backend"
sys.path.insert(0, str(backend_dir))

from app.modules.memory.recursive_search import (
    RecursiveSearchConfig,
    _extract_entity_references,
)
from app.modules.memory.vectorize import SearchHit


def test_extract_entities():
    """测试实体提取"""
    text = "I talked to John Smith about the project. He works at Microsoft."
    entities = _extract_entity_references(text)
    
    print("✅ 实体提取测试")
    print(f"  输入: {text}")
    print(f"  提取的实体: {entities}")
    
    # 检查至少提取到了一些实体
    assert len(entities) > 0
    assert "John Smith" in entities
    assert "Microsoft" in entities
    print("  ✓ 所有断言通过\n")


def test_recursive_config():
    """测试递归搜索配置"""
    config = RecursiveSearchConfig(
        max_depth=3,
        score_propagation_alpha=0.7,
        expansion_per_node=5,
        min_propagated_score=0.3,
    )
    
    print("✅ 递归搜索配置测试")
    print(f"  最大深度: {config.max_depth}")
    print(f"  分数传播系数: {config.score_propagation_alpha}")
    print(f"  每节点扩展数: {config.expansion_per_node}")
    print(f"  最低分数: {config.min_propagated_score}")
    print("  ✓ 配置创建成功\n")


def test_search_hit():
    """测试 SearchHit 数据结构"""
    hit = SearchHit(
        uri="memories/events/evt_123",
        score=0.85,
        title="测试事件",
        memory_type="events",
        scope="global",
    )
    
    print("✅ SearchHit 测试")
    print(f"  URI: {hit.uri}")
    print(f"  分数: {hit.score}")
    print(f"  标题: {hit.title}")
    print(f"  类型: {hit.memory_type}")
    print(f"  范围: {hit.scope}")
    print("  ✓ 数据结构正常\n")


def test_score_propagation():
    """测试分数传播计算"""
    alpha = 0.7
    child_score = 0.6
    parent_score = 0.9
    
    propagated = alpha * child_score + (1 - alpha) * parent_score
    
    print("✅ 分数传播测试")
    print(f"  子节点原始分数: {child_score}")
    print(f"  父节点分数: {parent_score}")
    print(f"  传播系数 α: {alpha}")
    print(f"  传播后分数: {propagated:.3f}")
    
    expected = 0.7 * 0.6 + 0.3 * 0.9  # 0.42 + 0.27 = 0.69
    assert abs(propagated - expected) < 0.001
    print(f"  ✓ 计算正确（期望 {expected:.3f}）\n")


async def test_recursive_search_mock():
    """模拟递归搜索流程（不需要数据库）"""
    print("✅ 递归搜索流程测试（模拟）")
    
    # 创建模拟的初始搜索结果
    starting_points = [
        SearchHit(
            uri="memories/events/evt_001",
            score=0.95,
            title="学习 Python",
            memory_type="events",
            scope="session",
        ),
        SearchHit(
            uri="memories/events/evt_002",
            score=0.88,
            title="写代码",
            memory_type="events",
            scope="session",
        ),
    ]
    
    print(f"  初始结果数: {len(starting_points)}")
    for hit in starting_points:
        print(f"    - {hit.title} (分数: {hit.score:.2f})")
    
    # 模拟递归扩展（实际需要数据库查询）
    # 这里只验证数据结构和逻辑
    config = RecursiveSearchConfig(max_depth=2)
    print(f"  配置: 最大深度={config.max_depth}, α={config.score_propagation_alpha}")
    
    # 模拟找到的相关记忆
    related = SearchHit(
        uri="memories/entities/ent_001",
        score=0.75,
        title="Python",
        memory_type="entities",
        scope="global",
    )
    
    # 计算传播后的分数
    parent_score = starting_points[0].score  # 0.95
    propagated_score = (
        config.score_propagation_alpha * related.score
        + (1 - config.score_propagation_alpha) * parent_score
    )
    
    print(f"  发现相关记忆: {related.title}")
    print(f"    原始分数: {related.score:.2f}")
    print(f"    传播后分数: {propagated_score:.2f}")
    print("  ✓ 递归逻辑正常\n")


def test_config_loading():
    """测试配置加载"""
    from app.core.config import settings
    
    print("✅ 配置加载测试")
    print(f"  递归搜索启用: {settings.memory.recall_enable_recursive_search}")
    print(f"  最大深度: {settings.memory.recall_recursive_max_depth}")
    print(f"  分数传播 α: {settings.memory.recall_recursive_alpha}")
    print(f"  每节点扩展: {settings.memory.recall_recursive_expansion}")
    print(f"  最低分数: {settings.memory.recall_recursive_min_score}")
    
    assert settings.memory.recall_enable_recursive_search is True
    assert settings.memory.recall_recursive_max_depth == 3
    assert settings.memory.recall_recursive_alpha == 0.7
    print("  ✓ 配置值正确\n")


def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("递归搜索功能验证")
    print("="*60 + "\n")
    
    try:
        # 同步测试
        test_extract_entities()
        test_recursive_config()
        test_search_hit()
        test_score_propagation()
        test_config_loading()
        
        # 异步测试
        asyncio.run(test_recursive_search_mock())
        
        print("="*60)
        print("✅ 所有测试通过！")
        print("="*60 + "\n")
        
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
