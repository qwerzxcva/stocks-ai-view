"""Explainable, evidence-gated good-stock scoring."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Dict, Iterable, Mapping, Optional


DEFAULT_MAX_AGE_HOURS = 24.0


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(section: Mapping[str, Any], *names: str) -> Optional[float]:
    for name in names:
        value = section.get(name)
        if _finite(value):
            return float(value)
    return None


def _text(section: Mapping[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = section.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def _asof(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ratio(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return value / 100.0 if value > 1 else value


def _section(record: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = record.get(name)
    return value if isinstance(value, Mapping) else {}


def _field(record: Mapping[str, Any], section: str, *names: str) -> Optional[float]:
    value = _number(_section(record, section), *names)
    if value is not None:
        return value
    return _number(record, *names)


def _quality_score(value: Optional[float], good: float, weak: float = 0.0) -> float:
    if value is None:
        return 0.0
    if value >= good:
        return 1.0
    if value > weak:
        return (value - weak) / (good - weak)
    return 0.0


def _freshness(record: Mapping[str, Any], now: datetime, max_age_hours: float):
    quote = _section(record, "quote")
    provenance = _section(record, "provenance")
    raw_values = (_text(quote, "asof", "ts", "timestamp"),
                  _text(record, "asof", "ts", "timestamp"),
                  _text(provenance, "snapshot_ts", "asof"))
    supplied = [value for value in raw_values if value]
    stamps = [_asof(value) for value in supplied]
    if not stamps or any(stamp is None for stamp in stamps):
        return False, None, "quote timestamp is missing or invalid"
    ages = [(now - stamp.astimezone(timezone.utc)).total_seconds() / 3600.0 for stamp in stamps]
    oldest_age = max(ages)
    if min(ages) < -1.0:
        return False, stamps[0], "quote timestamp is in the future"
    if oldest_age > max_age_hours:
        return False, stamps[0], f"quote snapshot is stale ({max(oldest_age, 0.0):.1f}h old)"
    return True, stamps[0], None


def _adjustment(record: Mapping[str, Any]) -> bool:
    adjustment = _section(record, "adjustment")
    provenance = _section(record, "provenance")
    status = (_text(adjustment, "status", "adjustment_status") or
              _text(record, "adjustment_status", "adjustment") or
              _text(provenance, "adjustment_status"))
    safe = adjustment.get("safe_for_long_term")
    if safe is None:
        safe = record.get("safe_for_long_term")
    accepted = {"ok", "safe", "verified", "trusted", "adjusted", "hfq_verified"}
    normalized = status.lower().replace("-", "_").replace(" ", "_") if status else None
    if isinstance(safe, bool) and not safe:
        return False
    if normalized is not None and normalized not in accepted:
        return False
    if isinstance(safe, bool):
        return safe
    return normalized in accepted


def _source_conflict(record: Mapping[str, Any]) -> bool:
    provenance = _section(record, "provenance")
    if provenance.get("source_conflict") is True or record.get("source_conflict") is True:
        return True
    conflicts = provenance.get("conflicts", record.get("conflicts"))
    return bool(conflicts)


def _missing(record: Mapping[str, Any], adjustment_safe: bool) -> list[str]:
    missing = []
    quote = _section(record, "quote")
    fundamentals = _section(record, "fundamentals")
    technical = _section(record, "technical")
    provenance = _section(record, "provenance")
    price = _field(record, "quote", "price", "close")
    if price is None or price <= 0:
        missing.append("quote.price")
    if not (_text(quote, "asof", "ts", "timestamp") or _text(record, "asof", "ts", "timestamp") or
            _text(provenance, "snapshot_ts", "asof")):
        missing.append("quote.asof")
    for key, aliases in {
        "fundamentals.eps": ("eps", "basic_eps", "diluted_eps"),
        "fundamentals.net_income": ("net_income", "net_profit"),
        "fundamentals.operating_cash_flow": ("operating_cash_flow", "ocf", "cash_flow"),
        "fundamentals.debt_to_equity": ("debt_to_equity", "debt_equity"),
    }.items():
        if _number(fundamentals, *aliases) is None and _number(record, *aliases) is None:
            missing.append(key)
    debt = _field(record, "fundamentals", "debt_to_equity", "debt_equity")
    if debt is not None and debt < 0:
        missing.append("fundamentals.debt_to_equity")
    # 财报完整性: 给了任何财报数字但没有报告期 → 数据不完整 (2026-10-18)
    fundamental_keys = ("eps", "basic_eps", "net_income", "net_profit",
                        "operating_cash_flow", "ocf", "debt_to_equity",
                        "debt_equity", "pe", "pe_ttm", "pb", "pb_mrq")
    has_fund = any(_number(fundamentals, k) is not None for k in fundamental_keys)
    period = (_text(fundamentals, "period_end", "report_date", "report_period")
              or _text(record, "period_end", "report_date", "report_period"))
    if has_fund and not period:
        missing.append("fundamentals.report_period")
    if not adjustment_safe:
        missing.append("technical.long_term_adjustment_verified")
    for key, aliases in {
        "technical.trend_score": ("trend_score", "trend"),
        "technical.location_score": ("location_score", "location"),
        "technical.volume_score": ("volume_score", "volume"),
    }.items():
        if _number(technical, *aliases) is None and _number(record, *aliases) is None:
            missing.append(key)
    return missing


def score_stock(record: Mapping[str, Any], *, now: Any = None,
                max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> Dict[str, Any]:
    """Return a per-stock score without turning absent evidence into a forecast."""
    if not isinstance(record, Mapping):
        raise TypeError("record must be a mapping")
    current = _asof(now) if now is not None else datetime.now(timezone.utc)
    if current is None:
        raise ValueError("now must be an ISO timestamp or datetime")
    if not _finite(max_age_hours) or max_age_hours < 0:
        raise ValueError("max_age_hours must be a finite non-negative number")
    current = current.astimezone(timezone.utc)
    quote = _section(record, "quote")
    fundamentals = _section(record, "fundamentals")
    technical = _section(record, "technical")
    valuation = _section(record, "valuation")
    edge = _section(record, "empirical_edge")
    provenance = dict(_section(record, "provenance"))
    fresh, stamp, freshness_reason = _freshness(record, current, max_age_hours)
    adjustment_safe = _adjustment(record)
    conflict = _source_conflict(record)
    missing = _missing(record, adjustment_safe)
    reasons: list[str] = []
    risks: list[str] = []
    points = 0.0

    eps = _field(record, "fundamentals", "eps", "basic_eps", "diluted_eps")
    if eps is None:
        risks.append("earnings evidence is missing")
    elif eps > 0:
        points += 15
        reasons.append("positive EPS")
    else:
        risks.append("negative or zero EPS; valuation is not awarded")

    net_income = _field(record, "fundamentals", "net_income", "net_profit")
    if net_income is not None and net_income > 0:
        points += 8
        reasons.append("positive net income")
    elif net_income is None:
        risks.append("net income evidence is missing")
    else:
        risks.append("net income is not positive")

    ocf = _field(record, "fundamentals", "operating_cash_flow", "ocf", "cash_flow")
    if ocf is not None and ocf > 0:
        points += 12
        reasons.append("positive operating cash flow")
    elif ocf is None:
        risks.append("operating cash flow evidence is missing")
    else:
        risks.append("operating cash flow is not positive")

    # 分红证据: 有真实 DPS 才给股息维度分; 缺失只提示, 不拦 eligible
    # (不分红或数据缺失 ≠ 坏公司, 不能学 PE/PB 反推)
    dps = _field(record, "fundamentals", "dps", "dividend_per_share")
    if dps is not None:
        if dps > 0:
            points += 5
            reasons.append("positive dividend per share")
        else:
            risks.append("dividend per share is not positive")
    else:
        risks.append("dividend per share evidence is missing")

    debt = _field(record, "fundamentals", "debt_to_equity", "debt_equity")
    if debt is not None and debt >= 0:
        points += 8 * (1.0 if debt <= 1 else _quality_score(1.0 / debt, 1.0, 0.0))
        if debt <= 1:
            reasons.append("debt-to-equity is at or below 1")
        else:
            risks.append("debt-to-equity is elevated")
    else:
        risks.append("debt evidence is missing")

    pe = _field(record, "fundamentals", "pe", "pe_ttm")
    pb = _field(record, "fundamentals", "pb", "pb_mrq")
    margin = _field(record, "fundamentals", "valuation_margin", "margin_of_safety")
    if margin is None:
        margin = _number(valuation, "valuation_margin", "margin_of_safety")
    valuation_price = _number(quote, "price", "close") or _number(record, "price", "close")
    intrinsic_value = _number(valuation, "intrinsic_value", "value")
    dev_pct = _number(valuation, "dev_pct", "value_dev_pct")
    if margin is None and intrinsic_value is not None and intrinsic_value > 0 and valuation_price is not None and valuation_price > 0:
        margin = (intrinsic_value - valuation_price) / intrinsic_value
    if margin is None and dev_pct is not None:
        margin = -dev_pct / 100.0
    if eps is not None and eps > 0 and pe is not None and pe > 0:
        points += 8 * max(0.0, min(1.0, (30.0 - pe) / 30.0))
        if pe <= 20:
            reasons.append("PE is within the valuation scoring range")
        else:
            risks.append("PE is high")
    else:
        missing.append("valuation.pe_with_positive_eps")
    if eps is not None and eps > 0 and pb is not None and pb > 0:
        points += 6 * max(0.0, min(1.0, (4.0 - pb) / 4.0))
    else:
        missing.append("valuation.pb_with_positive_eps")
    if eps is not None and eps > 0 and pe is not None and pe > 0 and pb is not None and pb > 0 and margin is not None:
        points += 4 * max(0.0, min(1.0, margin if margin <= 1 else margin / 100.0))
        reasons.append("valuation margin is backed by EPS, PE and PB")
    elif margin is not None and eps is not None and eps > 0 and intrinsic_value is not None and intrinsic_value > 0:
        points += 4 * max(0.0, min(1.0, margin))
        reasons.append("valuation margin backed by intrinsic value and positive EPS")
    elif margin is not None:
        risks.append("valuation margin ignored without real EPS, PE and PB or intrinsic value")

    if adjustment_safe:
        for key, aliases, weight, label in (
            ("trend_score", ("trend_score", "trend"), 10, "trend evidence"),
            ("location_score", ("location_score", "location"), 8, "location evidence"),
        ):
            value = _field(record, "technical", *aliases)
            if value is not None:
                points += weight * max(0.0, min(1.0, _ratio(value) or 0.0))
                if value > 0:
                    reasons.append(label)
    else:
        risks.append("day_qfq or adjustment provenance is unsafe; long-term indicators excluded")
    if adjustment_safe:
        volume = _field(record, "technical", "volume_score", "volume")
        if volume is not None:
            points += 6 * max(0.0, min(1.0, _ratio(volume) or 0.0))
            if volume > 0:
                reasons.append("volume evidence")

    edge_n = _number(edge, "n_samples", "samples", "sample_size")
    edge_pp = _number(edge, "edge_pp")
    edge_score = _number(edge, "edge_score", "score")
    edge_value = edge_pp if edge_pp is not None else edge_score
    if edge_value is None:
        missing.append("empirical_edge.edge_score")
    if edge_n is None:
        missing.append("empirical_edge.n_samples")
    elif edge_n < 100 or not edge_n.is_integer():
        missing.append("empirical_edge.n_samples")
        risks.append("empirical edge sample is too small")
    elif edge_pp is None and edge_score is None:
        missing.append("empirical_edge.edge_score")
    elif edge_pp is not None:
        normalized_edge = max(-1.0, min(1.0, edge_pp / 100.0))
        points += 15 * max(0.0, normalized_edge)
        if edge_pp > 0:
            reasons.append("positive empirical edge")
        elif edge_pp < 0:
            risks.append("negative empirical edge")
    else:
        normalized_edge = max(-1.0, min(1.0, edge_score / 100.0 if abs(edge_score) > 1 else edge_score))
        points += 15 * max(0.0, normalized_edge)
        if edge_score > 0:
            reasons.append("positive empirical edge")
        elif edge_score < 0:
            risks.append("negative empirical edge")
    if edge.get("bull_resonance") is not None or edge.get("bull_votes") is not None:
        risks.append("bull resonance is not treated as empirical evidence")

    if any(field.startswith("fundamentals.") for field in missing):
        points = 0.0
    score = max(0, min(100, round(points)))
    grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "F"
    if conflict:
        risks.append("source conflict on supplied data")
        decision = "reject"
    elif eps is not None and eps <= 0:
        decision = "reject"
    elif not fresh:
        decision = "insufficient_data"
    elif missing or not adjustment_safe:
        decision = "insufficient_data"
    elif edge_value is not None and edge_value <= 0:
        decision = "watch"
    elif points >= 65:
        decision = "eligible"
    else:
        decision = "watch"
    if freshness_reason:
        risks.append(freshness_reason)
    if missing:
        missing = list(dict.fromkeys(missing))
    provenance.setdefault("source", provenance.get("source", "supplied record"))
    provenance["freshness_ok"] = fresh
    provenance["adjustment_safe_for_long_term"] = adjustment_safe
    provenance["source_conflict"] = conflict
    output_asof = stamp.isoformat() if stamp else None
    return {
        "code": record.get("code") or record.get("symbol"),
        "score": score,
        "grade": grade,
        "decision": decision,
        "status": decision,
        "reasons": reasons,
        "risks": risks,
        "missing_fields": missing,
        "provenance": provenance,
        "asof": output_asof,
    }


def score_stocks(records: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[Dict[str, Any]]:
    return [score_stock(record, **kwargs) for record in records]


build_good_stock_score = score_stock
