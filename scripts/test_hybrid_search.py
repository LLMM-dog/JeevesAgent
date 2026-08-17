"""
验证混合搜索功能的测试脚本。
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
backend_dir = project_root / "backend"
sys.path.insert(0, str(backend_dir))


def test_bm25_tokenization():
    """测试 BM25 分词"""
    from app.infra.bm25 import BM25Index
    
    print("✅ BM25 分词测试")
    
    index = BM25Index()
    
    # 测试英文
    tokens = index._tokenize("Python 3.11 async programming")
    print(f"  英文分词: {tokens}")
    assert "python3" in tokens or "python" in tokens
    assert "11" in tokens
    assert "async" in tokens
    
    # 测试中文（简化分词，按字符）
    tokens = index._tokenize("学习 Python 异步编程")
    print(f"  中文分词: {tokens}")
    assert "python" in tokens
    
    print("  ✓ 分词正确\n")


def test_bm25_indexing():
    """测试 BM25 索引构建"""
    from app.infra.bm25 import BM25Index
    
    print("✅ BM25 索引测试")
    
    index = BM25Index()
    
    # 添加文档
    index.add_document("doc1", "Python 3.11 新特性")
    index.add_document("doc2", "Python 异步编程教程")
    index.add_document("doc3", "Java 编程入门")
    
    print(f"  文档数: {index.num_docs}")
    print(f"  平均长度: {index.avg_doc_length:.1f}")
    
    assert index.num_docs == 3
    assert index.avg_doc_length > 0
    
    # 搜索
    scores = index.search("Python 3.11")
    print(f"  搜索 'Python 3.11': {scores}")
    
    # doc1 应该得分最高（精确匹配 3.11）
    assert "doc1" in scores
    assert scores["doc1"] > scores.get("doc2", 0)
    
    print("  ✓ 索引和搜索正确\n")


def test_bm25_idf():
    """测试 IDF 计算"""
    from app.infra.bm25 import BM25Index
    
    print("✅ BM25 IDF 测试")
    
    index = BM25Index()
    
    # 添加文档
    index.add_document("doc1", "Python is great")
    index.add_document("doc2", "Python is powerful")
    index.add_document("doc3", "Java is popular")
    
    # "Python" 出现在 2/3 的文档中，IDF 应该较低
    idf_python = index._calculate_idf("python")
    
    # "great" 只出现在 1/3 的文档中，IDF 应该较高
    idf_great = index._calculate_idf("great")
    
    print(f"  IDF(python): {idf_python:.3f}")
    print(f"  IDF(great): {idf_great:.3f}")
    
    # 更稀有的词 IDF 更高
    assert idf_great > idf_python
    
    print("  ✓ IDF 计算正确\n")


def test_score_normalization():
    """测试分数归一化"""
    from app.infra.bm25 import normalize_scores
    
    print("✅ 分数归一化测试")
    
    raw_scores = {
        "doc1": 5.2,
        "doc2": 3.8,
        "doc3": 7.1,
    }
    
    normalized = normalize_scores(raw_scores)
    
    print(f"  原始分数: {raw_scores}")
    print(f"  归一化分数: {normalized}")
    
    # 检查范围 [0, 1]
    for score in normalized.values():
        assert 0.0 <= score <= 1.0
    
    # 最高分应该是 1.0
    assert max(normalized.values()) == 1.0
    
    # 最低分应该是 0.0
    assert min(normalized.values()) == 0.0
    
    print("  ✓ 归一化正确\n")


def test_query_analysis():
    """测试查询分析"""
    from app.infra.hybrid_search import QueryAnalyzer
    
    print("✅ 查询分析测试")
    
    # 版本号查询
    qtype = QueryAnalyzer.analyze("Python 3.11 新特性")
    print(f"  'Python 3.11 新特性' -> {qtype}")
    assert qtype == "keyword"
    
    # 代码片段查询
    qtype = QueryAnalyzer.analyze("如何使用 asyncio.run() 函数")
    print(f"  '如何使用 asyncio.run() 函数' -> {qtype}")
    assert qtype == "keyword"
    
    # 缩写查询
    qtype = QueryAnalyzer.analyze("API 设计最佳实践")
    print(f"  'API 设计最佳实践' -> {qtype}")
    assert qtype == "keyword"
    
    # 语义查询
    qtype = QueryAnalyzer.analyze("如何提高代码质量")
    print(f"  '如何提高代码质量' -> {qtype}")
    assert qtype == "semantic"
    
    # 平衡查询
    qtype = QueryAnalyzer.analyze("Python 编程技巧")
    print(f"  'Python 编程技巧' -> {qtype}")
    assert qtype == "balanced"
    
    print("  ✓ 查询分析正确\n")


async def test_score_mixing():
    """测试分数混合"""
    from app.infra.hybrid_search import mix_scores
    
    print("✅ 分数混合测试")
    
    dense_scores = {
        "doc1": 0.8,
        "doc2": 0.6,
        "doc3": 0.7,
    }
    
    sparse_scores = {
        "doc2": 0.9,  # doc2 在 BM25 中得分高
        "doc3": 0.5,
        "doc4": 0.8,  # doc4 只在 BM25 中出现
    }
    
    # 混合 (0.7 密集 + 0.3 稀疏)
    mixed = mix_scores(dense_scores, sparse_scores, 0.7, 0.3)
    
    print(f"  密集分数: {dense_scores}")
    print(f"  稀疏分数: {sparse_scores}")
    print(f"  混合分数: {mixed}")
    
    # 验证计算
    # doc1: 0.7 * 0.8 + 0.3 * 0 = 0.56
    assert abs(mixed["doc1"] - 0.56) < 0.01
    
    # doc2: 0.7 * 0.6 + 0.3 * 0.9 = 0.69
    assert abs(mixed["doc2"] - 0.69) < 0.01
    
    # doc4: 0.7 * 0 + 0.3 * 0.8 = 0.24
    assert abs(mixed["doc4"] - 0.24) < 0.01
    
    print("  ✓ 混合计算正确\n")


async def test_hybrid_search():
    """测试混合搜索流程"""
    from app.infra.hybrid_search import HybridSearchConfig, hybrid_search
    
    print("✅ 混合搜索流程测试")
    
    # 模拟场景：查询 "Python 3.11"
    query = "Python 3.11 新特性"
    
    dense_scores = {
        "doc1": 0.85,  # Python 教程（语义相关）
        "doc2": 0.78,  # Python 3.11 文档（标题匹配）
        "doc3": 0.65,  # 编程语言对比
    }
    
    sparse_scores = {
        "doc2": 0.95,  # 精确匹配 "3.11"
        "doc1": 0.40,  # 只匹配 "Python"
        "doc3": 0.20,
    }
    
    config = HybridSearchConfig()
    mixed = await hybrid_search(query, dense_scores, sparse_scores, config)
    
    print(f"  查询: {query}")
    print(f"  混合分数: {mixed}")
    
    # doc2 应该得分最高（密集和稀疏都高）
    sorted_docs = sorted(mixed.items(), key=lambda x: -x[1])
    print(f"  排序结果: {[doc for doc, _ in sorted_docs]}")
    
    assert sorted_docs[0][0] == "doc2"
    
    print("  ✓ 混合搜索正确\n")


async def test_adaptive_hybrid():
    """测试自适应混合"""
    from app.infra.hybrid_search import adaptive_hybrid_search
    
    print("✅ 自适应混合测试")
    
    # 场景 1: 高重叠（密集和稀疏结果一致）
    dense_scores_high = {"doc1": 0.9, "doc2": 0.8, "doc3": 0.7}
    sparse_scores_high = {"doc1": 0.85, "doc2": 0.75, "doc3": 0.65}
    
    mixed_high = await adaptive_hybrid_search(
        "test query", dense_scores_high, sparse_scores_high
    )
    print(f"  高重叠场景: {mixed_high}")
    
    # 场景 2: 低重叠（密集和稀疏结果不同）
    dense_scores_low = {"doc1": 0.9, "doc2": 0.8}
    sparse_scores_low = {"doc3": 0.85, "doc4": 0.75}
    
    mixed_low = await adaptive_hybrid_search(
        "test query", dense_scores_low, sparse_scores_low
    )
    print(f"  低重叠场景: {mixed_low}")
    
    # 验证都有结果
    assert len(mixed_high) > 0
    assert len(mixed_low) > 0
    
    print("  ✓ 自适应混合正确\n")


def test_config_loading():
    """测试配置加载"""
    from app.core.config import settings
    
    print("✅ 混合搜索配置测试")
    print(f"  启用: {settings.memory.recall_enable_hybrid_search}")
    print(f"  BM25 k1: {settings.memory.bm25_k1}")
    print(f"  BM25 b: {settings.memory.bm25_b}")
    print(f"  混合策略: {settings.memory.hybrid_search_strategy}")
    print(f"  默认密集权重: {settings.memory.hybrid_default_dense_weight}")
    print(f"  默认稀疏权重: {settings.memory.hybrid_default_sparse_weight}")
    print(f"  关键词密集权重: {settings.memory.hybrid_keyword_dense_weight}")
    print(f"  关键词稀疏权重: {settings.memory.hybrid_keyword_sparse_weight}")
    
    # 验证权重和为 1.0
    total_default = (
        settings.memory.hybrid_default_dense_weight
        + settings.memory.hybrid_default_sparse_weight
    )
    total_keyword = (
        settings.memory.hybrid_keyword_dense_weight
        + settings.memory.hybrid_keyword_sparse_weight
    )
    
    assert abs(total_default - 1.0) < 0.001
    assert abs(total_keyword - 1.0) < 0.001
    
    print("  ✓ 配置值正确\n")


def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("混合搜索功能验证")
    print("="*60 + "\n")
    
    try:
        # 同步测试
        test_bm25_tokenization()
        test_bm25_indexing()
        test_bm25_idf()
        test_score_normalization()
        test_query_analysis()
        test_config_loading()
        
        # 异步测试
        asyncio.run(test_score_mixing())
        asyncio.run(test_hybrid_search())
        asyncio.run(test_adaptive_hybrid())
        
        print("="*60)
        print("✅ 所有测试通过！")
        print("="*60 + "\n")
        
        print("💡 提示：要启用混合搜索，需要设置：")
        print("  JEEVES_MEMORY__RECALL_ENABLE_HYBRID_SEARCH=true")
        print()
        print("📖 混合搜索优势：")
        print("  - 密集向量：理解语义相似度（同义词、概念）")
        print("  - BM25 稀疏：精确关键词匹配（版本号、代码片段）")
        print("  - 智能混合：根据查询类型自适应调整权重")
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
