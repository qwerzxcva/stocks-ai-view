#!/usr/bin/env python3
"""picks_ledger.py — 推股追加式账本 (2026-10-19)

用户硬要求:
  - 早盘/午盘推股「实时更新, 只能新增不能删除, 需要标注时间」
  - 如需更改综合分/评级, 必须记录「更改时间 + 改前 ➡️ 改后」
  - 早盘/午盘时段过去后不能再变动 (冻结)
  - 明日候选只能在收盘后到开盘前 15 分钟变动
  - 明日候选必须与早盘推股使用不同算法/引擎, 可互相搭配但不能同一套

为什么单独做账本而不改 premarket_picks.json:
  现有 premarket_picks.json 每次跑都整体覆盖 (tiers 重写), 天然违反「只增不删」。
  账本是 append-only 的事件日志, 任何一次写入只追加记录, 不改历史行;
  读取时按时间倒序重放, 得到「当前有效状态」与「完整变更历史」两套视图。

记录类型 (kind):
  add     首次推荐某只股票 (带 session/engine/score/grade/time)
  update  同一只股票的综合分或评级发生变化 (from -> to, 附 change_reason)
  freeze  该 session 到期冻结 (之后任何 add/update 都被拒绝)

session 规则:
  premarket   09:15 起可写, 09:30 后只读 (早盘开盘)
  midday      11:30 起可写, 13:00 后只读
  tomorrow    15:00 起可写, 次日 09:15 后只读

用法:
  # 追加推荐
  python3 picks_ledger.py add --session premarket --code 600585 --name 海螺水泥 \
      --score 79.2 --grade A --price 18.64 --engine engine_v5 --reason "below_ma60"
  # 更新评分 (自动记录 from->to)
  python3 picks_ledger.py update --session premarket --code 600585 --score 81.0 --grade A+
  # 冻结
  python3 picks_ledger.py freeze --session premarket
  # 查看
  python3 picks_ledger.py show --session premarket
  python3 picks_ledger.py build          # 生成 data/picks_state.json 给前端
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

LEDGER = os.path.join(DATA_DIR, "picks_ledger.jsonl")
STATE = os.path.join(DATA_DIR, "picks_state.json")

# session -> (可写起始时刻, 可写截止时刻) 仅用于 show 时提示, 实际冻结靠 freeze 记录
SESSIONS = ("premarket", "midday", "tomorrow")

# 明日候选不得与早盘使用同一算法: 引擎白名单
ENGINE_RULES = {
    "premarket": {"allow": {"engine_v5", "multidim", "premarket_v1"}},
    "midday": {"allow": {"engine_v5", "multidim", "midday_v1"}},
    # tomorrow 必须用不同引擎: 排除纯 engine_v5
    "tomorrow": {"deny": {"engine_v5"}, "allow": {"ai_pick_v4", "scenario",
                                                  "tomorrow_v1", "multidim"}},
}


def _now():
    return datetime.now(CST)


def _read_all():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append(rec):
    os.makedirs(os.path.dirname(os.path.abspath(LEDGER)), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _is_frozen(records, session):
    return any(r.get("kind") == "freeze" and r.get("session") == session
               for r in records)


def _latest(records, session):
    """重放得到 session 当前有效状态: {code: {...}}"""
    state = {}
    for r in records:
        if r.get("session") != session:
            continue
        k = r.get("kind")
        if k == "add":
            c = r.get("code")
            state[c] = {
                "code": c, "name": r.get("name"),
                "score": r.get("score"), "grade": r.get("grade"),
                "price": r.get("price"), "engine": r.get("engine"),
                "reason": r.get("reason"),
                "first_added": r.get("ts"),
                "last_updated": r.get("ts"),
                "history": [{"ts": r.get("ts"), "kind": "add",
                             "score": r.get("score"), "grade": r.get("grade")}],
            }
        elif k == "update":
            c = r.get("code")
            if c not in state:
                continue
            cur = state[c]
            frm = {"score": cur["score"], "grade": cur["grade"]}
            to = {"score": r.get("score", cur["score"]),
                  "grade": r.get("grade", cur["grade"])}
            if frm == to:
                continue
            cur["score"] = to["score"]
            cur["grade"] = to["grade"]
            cur["last_updated"] = r.get("ts")
            cur["history"].append({
                "ts": r.get("ts"), "kind": "update",
                "from": frm, "to": to,
                "text": f"{frm['score']}({frm['grade']}) ➡️ "
                        f"{to['score']}({to['grade']})",
                "reason": r.get("change_reason"),
            })
    return state


def cmd_add(args):
    recs = _read_all()
    if _is_frozen(recs, args.session):
        print(f"[ledger] {args.session} 已冻结, 拒绝新增", file=sys.stderr)
        return 2
    rule = ENGINE_RULES.get(args.session) or {}
    eng = args.engine or ""
    if rule.get("deny") and eng in rule["deny"]:
        print(f"[ledger] {args.session} 不允许用引擎 {eng} "
              f"(必须与早盘不同), 拒绝", file=sys.stderr)
        return 2
    cur = _latest(recs, args.session)
    if args.code in cur:
        # 已存在 -> 走 update 语义, 不改首次记录
        args.score_from = cur[args.code]["score"]
        args.grade_from = cur[args.code]["grade"]
        return cmd_update(args, recs=recs)
    _append({
        "kind": "add", "session": args.session, "ts": _now().isoformat(timespec="seconds"),
        "code": args.code, "name": args.name, "score": args.score,
        "grade": args.grade, "price": args.price, "engine": eng,
        "reason": args.reason,
    })
    print(f"[ledger] + {args.session} {args.code} {args.name} "
          f"score={args.score} grade={args.grade}")
    return 0


def cmd_update(args, recs=None):
    recs = recs if recs is not None else _read_all()
    if _is_frozen(recs, args.session):
        print(f"[ledger] {args.session} 已冻结, 拒绝更新", file=sys.stderr)
        return 2
    cur = _latest(recs, args.session)
    if args.code not in cur:
        print(f"[ledger] {args.code} 不在 {args.session}, 请先 add", file=sys.stderr)
        return 2
    old = cur[args.code]
    new_score = args.score if args.score is not None else old["score"]
    new_grade = args.grade if args.grade is not None else old["grade"]
    if new_score == old["score"] and new_grade == old["grade"]:
        print("[ledger] 分数与评级未变, 不记录")
        return 0
    _append({
        "kind": "update", "session": args.session,
        "ts": _now().isoformat(timespec="seconds"),
        "code": args.code, "score": new_score, "grade": new_grade,
        "change_reason": args.reason or "",
    })
    print(f"[ledger] ~ {args.session} {args.code} "
          f"{old['score']}({old['grade']}) ➡️ {new_score}({new_grade})")
    return 0


def cmd_freeze(args):
    recs = _read_all()
    if _is_frozen(recs, args.session):
        print(f"[ledger] {args.session} 已冻结")
        return 0
    _append({"kind": "freeze", "session": args.session,
             "ts": _now().isoformat(timespec="seconds"),
             "reason": args.reason or "session ended"})
    print(f"[ledger] freeze {args.session}")
    return 0


def cmd_show(args):
    recs = _read_all()
    sessions = [args.session] if args.session else list(SESSIONS)
    for s in sessions:
        state = _latest(recs, s)
        frozen = _is_frozen(recs, s)
        print(f"=== {s} ({len(state)} 只, {'已冻结' if frozen else '可写'}) ===")
        for c, v in sorted(state.items(),
                           key=lambda x: -(x[1].get("score") or 0)):
            print(f"  {c} {v.get('name') or '':8s} "
                  f"score={v.get('score')} grade={v.get('grade')} "
                  f"engine={v.get('engine')} added={v.get('first_added')}")
            for h in v.get("history", []):
                if h.get("kind") == "update":
                    print(f"      {h['ts']} {h['text']} ({h.get('reason') or ''})")
    return 0


def cmd_build(_args):
    recs = _read_all()
    out = {"ts": _now().isoformat(timespec="seconds"),
           "contract": "append_only_picks_ledger",
           "sessions": {}}
    for s in SESSIONS:
        state = _latest(recs, s)
        out["sessions"][s] = {
            "frozen": _is_frozen(recs, s),
            "n": len(state),
            "picks": sorted(state.values(),
                            key=lambda x: -(x.get("score") or 0)),
        }
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[ledger] -> {STATE} "
          f"({', '.join(f'{s}={out['sessions'][s]['n']}' for s in SESSIONS)})")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--session", default="premarket", choices=SESSIONS)
        p.add_argument("--code", required=True)
        p.add_argument("--name", default="")
        p.add_argument("--score", type=float)
        p.add_argument("--grade", default="")
        p.add_argument("--price", type=float)
        p.add_argument("--engine", default="")
        p.add_argument("--reason", default="")

    a = sub.add_parser("add"); common(a); a.set_defaults(func=cmd_add)
    u = sub.add_parser("update"); common(u); u.set_defaults(func=cmd_update)
    f = sub.add_parser("freeze")
    f.add_argument("--session", default="premarket", choices=SESSIONS)
    f.add_argument("--reason", default=""); f.set_defaults(func=cmd_freeze)
    s = sub.add_parser("show")
    s.add_argument("--session", choices=SESSIONS); s.set_defaults(func=cmd_show)
    b = sub.add_parser("build"); b.set_defaults(func=cmd_build)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help(); return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())