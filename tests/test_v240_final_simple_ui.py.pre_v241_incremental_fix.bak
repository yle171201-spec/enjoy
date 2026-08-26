from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_final_daily_navigation_is_simple():
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    assert "今日操作" in base
    assert "我的持仓" in base
    assert "历史记录" in base
    assert "数据状态" in base
    assert "/portfolio" not in base
    assert "/review" not in base
    assert "/backtest" not in base

def test_dashboard_uses_plain_language():
    s = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "今天怎么做" in s
    assert "核心买点" in s
    assert "今天没有新买点" in s
    assert "研究功能" not in s
    assert "Money-Wave" not in s
    assert "N2-R1" not in s

def test_strategy_math_versions_are_not_rewritten():
    cfg = (ROOT / "app/config.py").read_text(encoding="utf-8")
    assert 'strategy_version: str = "V18"' in cfg
    assert 'live_strategy_version: str = "V18-LIVE"' in cfg
