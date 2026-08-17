"""
验证热度评分功能的测试脚本。
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
backend_dir = project_root / "backend"
sys.path.insert(0, str(backend_dir))


def test_frequency_component():
    """测试频率分量计算"""
    from app.infra.hotness import frequency_component
    
    print("✅ 频率分量测试")
    
    # 测试不同访问次数
    test_cases = [
        (0, 0.5),     # 新记忆
        (1, 0.67),    # 访问 1 次
        (5, 0.86),    # 访问 5 次
        (10, 0.92),   # 访问 10 次
        (50, 0.98),   # 访问 50 次
        (100, 0.99),  # 访问 100 次
    ]
    
    for active_count, expected_approx in test_cases:
        freq = frequency_component(active_count)
        print(f"  访问 {active_count:3d} 次 → freq = {freq:.3f} (期望 ~{expected_approx:.3f})")
        
        # 允许一些误差
        assert abs(freq - expected_approx) < 0.05, f"频率分量计算错误: {freq} != {expected_approx}"
    
    print("  ✓ 频率分量计算正确\n")


def test_recency_component():
    """测试时间衰减分量"""
    from app.infra.hotness import recency_component
    
    print("✅ 时间衰减测试")
    
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    half_life = 7.0
    
    test_cases = [
        (0, 1.0),     # 刚更新
        (7, 0.5),     # 半衰期
        (14, 0.25),   # 两个半衰期
        (21, 0.125),  # 三个半衰期
        (30, 0.057),  # 一个月
    ]
    
    for days_ago, expected_approx in test_cases:
        updated_at = now - timedelta(days=days_ago)
        recency = recency_component(updated_at, now, half_life)
        print(f"  {days_ago:2d} 天前 → recency = {recency:.3f} (期望 ~{expected_approx:.3f})")
        
        assert abs(recency - expected_approx) < 0.01, f"时间衰减计算错误: {recency} != {expected_approx}"
    
    print("  ✓ 时间衰减计算正确\n")


def test_hotness_score():
    """测试完整热度分数"""
    from app.infra.hotness import hotness_score
    
    print("✅ 热度分数测试")
    
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    
    # 场景 1: 高频访问 + 最近更新 → 高热度
    updated_1 = now - timedelta(days=1)
    hotness_1 = hotness_score(20, updated_1, now, half_life_days=7.0)
    print(f"  场景 1: 访问 20 次, 1 天前 → hotness = {hotness_1:.3f}")
    assert hotness_1 > 0.8, "高频 + 最近 = 高热度"
    
    # 场景 2: 低频访问 + 最近更新 → 中等热度
    updated_2 = now - timedelta(days=1)
    hotness_2 = hotness_score(2, updated_2, now, half_life_days=7.0)
    print(f"  场景 2: 访问 2 次, 1 天前 → hotness = {hotness_2:.3f}")
    assert 0.5 < hotness_2 < 0.8, "低频 + 最近 = 中等热度"
    
    # 场景 3: 高频访问 + 很久以前 → 中等热度
    updated_3 = now - timedelta(days=30)
    hotness_3 = hotness_score(20, updated_3, now, half_life_days=7.0)
    print(f"  场景 3: 访问 20 次, 30 天前 → hotness = {hotness_3:.3f}")
    assert hotness_3 < 0.1, "高频 + 很久 = 低热度"
    
    # 场景 4: 低频访问 + 很久以前 → 低热度
    updated_4 = now - timedelta(days=30)
    hotness_4 = hotness_score(1, updated_4, now, half_life_days=7.0)
    print(f"  场景 4: 访问 1 次, 30 天前 → hotness = {hotness_4:.3f}")
    assert hotness_4 < 0.1, "低频 + 很久 = 低热度"
    
    print("  ✓ 热度分数计算正确\n")


def test_blend_with_hotness():
    """测试分数混合"""
    from app.infra.hotness import blend_with_hotness
    
    print("✅ 分数混合测试")
    
    # 场景 1: 高语义 + 高热度
    blended_1 = blend_with_hotness(0.9, 0.8, alpha=0.15)
    print(f"  语义 0.9 + 热度 0.8 (α=0.15) → {blended_1:.3f}")
    expected_1 = 0.9 * 0.85 + 0.8 * 0.15  # 0.765 + 0.12 = 0.885
    assert abs(blended_1 - expected_1) < 0.01
    
    # 场景 2: 低语义 + 高热度（热度提升）
    blended_2 = blend_with_hotness(0.5, 0.9, alpha=0.15)
    print(f"  语义 0.5 + 热度 0.9 (α=0.15) → {blended_2:.3f}")
    expected_2 = 0.5 * 0.85 + 0.9 * 0.15  # 0.425 + 0.135 = 0.56
    assert abs(blended_2 - expected_2) < 0.01
    
    # 场景 3: 高语义 + 低热度（语义主导）
    blended_3 = blend_with_hotness(0.9, 0.3, alpha=0.15)
    print(f"  语义 0.9 + 热度 0.3 (α=0.15) → {blended_3:.3f}")
    expected_3 = 0.9 * 0.85 + 0.3 * 0.15  # 0.765 + 0.045 = 0.81
    assert abs(blended_3 - expected_3) < 0.01
    
    print("  ✓ 分数混合正确\n")


def test_alpha_weight_effect():
    """测试不同 alpha 权重的影响"""
    from app.infra.hotness import blend_with_hotness
    
    print("✅ Alpha 权重影响测试")
    
    semantic = 0.7
    hotness = 0.9
    
    for alpha in [0.0, 0.15, 0.5, 1.0]:
        blended = blend_with_hotness(semantic, hotness, alpha)
        print(f"  α = {alpha:.2f} → blended = {blended:.3f}")
    
    # α = 0: 纯语义
    assert abs(blend_with_hotness(semantic, hotness, 0.0) - semantic) < 0.001
    
    # α = 1: 纯热度
    assert abs(blend_with_hotness(semantic, hotness, 1.0) - hotness) < 0.001
    
    print("  ✓ Alpha 权重影响正确\n")


def test_half_life_effect():
    """测试不同半衰期的影响"""
    from app.infra.hotness import recency_component
    
    print("✅ 半衰期影响测试")
    
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    updated_at = now - timedelta(days=14)
    
    for half_life in [3, 7, 14, 30]:
        recency = recency_component(updated_at, now, half_life)
        print(f"  半衰期 {half_life:2d} 天, 14 天前 → recency = {recency:.3f}")
    
    # 半衰期越短，衰减越快
    recency_short = recency_component(updated_at, now, 3)
    recency_long = recency_component(updated_at, now, 30)
    assert recency_short < recency_long, "短半衰期 → 更快衰减"
    
    print("  ✓ 半衰期影响正确\n")


def test_estimate_active_count():
    """测试访问次数估算"""
    from app.infra.hotness import estimate_active_count_for_freq, frequency_component
    
    print("✅ 访问次数估算测试")
    
    for target_freq in [0.7, 0.8, 0.9, 0.95]:
        estimated_count = estimate_active_count_for_freq(target_freq)
        actual_freq = frequency_component(estimated_count)
        print(f"  目标 freq = {target_freq:.2f} → 需要 ~{estimated_count} 次访问 (实际 {actual_freq:.3f})")
        
        # 估算应该接近目标
        assert abs(actual_freq - target_freq) < 0.05
    
    print("  ✓ 访问次数估算正确\n")


def test_batch_hotness():
    """测试批量热度计算"""
    from app.infra.hotness import batch_hotness_scores
    
    print("✅ 批量热度计算测试")
    
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    
    items = [
        (10, now - timedelta(days=1)),   # 高频 + 最近
        (2, now - timedelta(days=7)),    # 低频 + 半衰期
        (20, now - timedelta(days=30)),  # 高频 + 很久
    ]
    
    scores = batch_hotness_scores(items, now, half_life_days=7.0)
    
    print(f"  批量计算 {len(items)} 个记忆")
    for i, ((count, updated_at), score) in enumerate(zip(items, scores, strict=False)):
        days_ago = (now - updated_at).days
        print(f"    记忆 {i+1}: 访问 {count:2d} 次, {days_ago:2d} 天前 → {score:.3f}")
    
    # 验证排序（第一个应该最高）
    assert scores[0] > scores[1] > scores[2]
    
    print("  ✓ 批量计算正确\n")


def test_config_loading():
    """测试配置加载"""
    from app.core.config import settings
    
    print("✅ 热度配置测试")
    print(f"  热度权重 (alpha): {settings.memory.hotness_weight}")
    print(f"  半衰期 (天): {settings.memory.hotness_half_life_days}")
    
    # 验证默认值
    assert settings.memory.hotness_weight == 0.15
    assert settings.memory.hotness_half_life_days == 7.0
    
    print("  ✓ 配置值正确\n")


def test_realistic_scenario():
    """测试现实场景"""
    from app.infra.hotness import blend_with_hotness, hotness_score
    
    print("✅ 现实场景测试")
    
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    
    # 记忆 A: 高语义相关 (0.85)，但很少被访问 (2 次)，1 周前更新
    semantic_a = 0.85
    hotness_a = hotness_score(2, now - timedelta(days=7), now, 7.0)
    blended_a = blend_with_hotness(semantic_a, hotness_a, 0.15)
    
    # 记忆 B: 中等语义相关 (0.70)，但经常被访问 (20 次)，昨天更新
    semantic_b = 0.70
    hotness_b = hotness_score(20, now - timedelta(days=1), now, 7.0)
    blended_b = blend_with_hotness(semantic_b, hotness_b, 0.15)
    
    print(f"  记忆 A: 语义 {semantic_a:.2f}, 热度 {hotness_a:.3f} → 混合 {blended_a:.3f}")
    print(f"  记忆 B: 语义 {semantic_b:.2f}, 热度 {hotness_b:.3f} → 混合 {blended_b:.3f}")
    
    # 在 alpha=0.15 下，语义仍然主导
    # 但如果语义差距不大，高热度可以提升排名
    if semantic_a - semantic_b < 0.2:  # 语义差距小
        print("  → 语义差距小时，热度可以改变排名")
    
    print("  ✓ 现实场景测试完成\n")


def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("热度评分功能验证")
    print("="*60 + "\n")
    
    try:
        test_frequency_component()
        test_recency_component()
        test_hotness_score()
        test_blend_with_hotness()
        test_alpha_weight_effect()
        test_half_life_effect()
        test_estimate_active_count()
        test_batch_hotness()
        test_config_loading()
        test_realistic_scenario()
        
        print("="*60)
        print("✅ 所有测试通过！")
        print("="*60 + "\n")
        
        print("💡 热度评分工作原理：")
        print("  1. 频率分量：访问次数越多，频率越高（sigmoid 变换）")
        print("  2. 时间衰减：距离上次更新越久，热度越低（指数衰减）")
        print("  3. 热度 = 频率 × 时间衰减")
        print("  4. 最终分数 = (1-α)*语义分数 + α*热度")
        print()
        print("📊 默认配置：")
        print("  - α (热度权重) = 0.15")
        print("  - 半衰期 = 7 天")
        print()
        print("🎯 效果：")
        print("  - 语义仍然主导（85%）")
        print("  - 热度作为辅助信号（15%）")
        print("  - 防止霸榜效应（新记忆也有机会）")
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
