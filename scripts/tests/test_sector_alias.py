"""板块别名表回归测试。

背景（2026-10-27）：sector_engine 的 27 个板块对 stock_sector_map 的 127 个板块，
直接命中只有 21 个，6 个对不上 —— 它们的个股在 App 里全部落到「未知板块」：

    白酒 / 国有大型银行 / 轨交设备 / 电视广播 / 地面兵装 / 专业连锁

根因：东财新分类给二级行业统一加了「Ⅱ」后缀（白酒Ⅱ、轨交设备Ⅱ …），
而 sector_engine 用的是旧名；「国有大型银行」还多一层层级错位（三级 vs 二级）。

这里锁三件事：
  1. 归一化后 27 个板块必须全部有归属，一个都不能再落空；
  2. 不能误配 ——「非白酒」的成员绝不能并进白酒；
  3. 白名单要生效 —— 招行、兰州银行不能被说成国有大型银行。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import build_stock_sector_map as bsm

DATA = HERE.parent / "data"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stock_sector_snapshot.json"

# 这次人工确认要补齐的 6 个
SIX = ("白酒", "国有大型银行", "轨交设备", "电视广播", "地面兵装", "专业连锁")


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _snapshot():
    return _load(FIXTURE)


def _engine_sectors():
    return [r["sector"] for r in _load(DATA / "sector_engine.json")["sectors"]]


def test_alias_table_is_wellformed():
    """别名表本身：6 条、都有 to、都有 confidence。"""
    doc = _load(DATA / "sector_alias.json")
    aliases = doc["aliases"]
    assert set(aliases) == set(SIX), f"别名表应恰好覆盖这 6 个板块，实际 {sorted(aliases)}"
    for name, spec in aliases.items():
        assert spec.get("to"), f"{name} 缺 to"
        assert spec.get("confidence") in ("high", "partial"), f"{name} confidence 取值异常"
        assert spec.get("why"), f"{name} 缺人工确认依据"


def test_all_27_engine_sectors_have_members_after_alias():
    """核心断言：归一化后，27 个板块必须每一个都能被映射到的股票覆盖。"""
    snap = _snapshot()
    rules = bsm.load_alias_rules()
    mapped = bsm.apply_alias(snap["map"], rules)
    covered = set(mapped.values())
    missing = [s for s in _engine_sectors() if s not in covered]
    assert missing == [], f"归一化后仍有板块没有任何成分股：{missing}"


def test_alias_rules_actually_fire():
    """别名规则必须真的改写了板块名，而不是形同虚设。"""
    snap = _snapshot()
    rules = bsm.load_alias_rules()
    before = set(snap["map"].values())
    for target in ("白酒Ⅱ", "轨交设备Ⅱ", "电视广播Ⅱ", "地面兵装Ⅱ", "专业连锁Ⅱ"):
        if target in before:
            assert target in rules, f"{target} 应命中别名规则"
    assert "白酒" in set(bsm.apply_alias(snap["map"], rules).values())


def test_non_baijiu_is_not_merged_into_baijiu():
    """防错配：『非白酒』装的是黄酒/啤酒，并进白酒就是方向性错误。"""
    snap = _snapshot()
    mapped = bsm.apply_alias(snap["map"], bsm.load_alias_rules())
    for code in ("000752", "000929", "600059", "600084"):
        if code in mapped:
            assert mapped[code] != "白酒", f"{code} 属于非白酒，被错并进白酒了"


def test_bank_whitelist_rejects_joint_stock_banks():
    """银行Ⅱ 是二级行业，只认六大行；招行/兰州银行不得被标成国有大型银行。"""
    rules = bsm.load_alias_rules()
    out = bsm.apply_alias(
        {"601398": "银行Ⅱ", "601288": "银行Ⅱ", "600036": "银行Ⅱ", "001227": "银行Ⅱ"},
        rules,
    )
    assert out["601398"] == "国有大型银行"   # 工商银行
    assert out["601288"] == "国有大型银行"   # 农业银行
    assert out["600036"] == "银行Ⅱ"          # 招商银行：股份行
    assert out["001227"] == "银行Ⅱ"          # 兰州银行：城商行


def test_alias_leaves_unrelated_names_untouched():
    """别名表不能顺手改动别的板块名。"""
    rules = bsm.load_alias_rules()
    out = bsm.apply_alias({"600519": "白酒Ⅱ", "000001": "银行Ⅱ", "300750": "电池"}, rules)
    assert out["300750"] == "电池"


def test_market_sort_is_by_code_not_by_change():
    """抓取按代码排序，不按涨幅。按涨幅会系统性丢掉茅台/建行这类大票。"""
    assert bsm.MARKET_FID == "f12", "全市场抓取必须按代码排序（fid=f12）"
