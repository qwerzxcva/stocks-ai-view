#!/usr/bin/env python3
"""engine_v5_nolookahead.py — engine_v5 的无前视 (no-lookahead) 包装器。

瓶颈 1 修复: scripts/engine_v5.py 不透传 asof, 回测时永远用「当前最新 K 线」
打分 → 前视偏差。R3 集成回测因此用 --no-v5 规避, 集成只剩 2 引擎。

设计 (不改 engine_v5.py 本体, 本体可能被另一个 AI 使用):
  - 无 --asof: 直接转调 engine_v5.main(), 行为与本体逐字节一致 (回归保证)。
  - 有 --asof: monkeypatch engine_v5.load_klines, 在其返回的每只 K 线上
    按 date <= asof (含当日) 截断, 再调 engine_v5.main()。打分/排序/输出
    逻辑 100% 复用本体, 只有数据面被截断 → 严格无前视。
  - asof 模式强制 --no-ext: all_sources.json / moneyflow.json 是「当前快照」
    文件, 无历史序列, 回测时使用即为前视。截断 K 线做不到的事不假装做到。

CLI 与 engine_v5 对齐 (--bulk/--out/--limit/--min-score/--no-ext/
--main-board-only), 额外加 --asof YYYY-MM-DD。

截断语义与 strategies/_loader.py 的 slice_asof 一致 (<= asof 含当日);
K 线结构不同 (本体是 list-of-rows, _loader 是列式 dict), 故此处对
[date,open,close,high,low,vol] 行列表做等价截断, 不重复实现打分逻辑。

用法:
  # 回归: 等价于 python3 scripts/engine_v5.py --out x.json
  python3 scripts/engines/engine_v5_nolookahead.py --out x.json

  # 无前视: 站在 2024-09-24 收盘后打分
  python3 scripts/engines/engine_v5_nolookahead.py --asof 2024-09-24 --out x.json
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, ".."))
for _p in (SCRIPTS, os.path.join(SCRIPTS, "engines", "strategies")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _loader  # noqa: E402  (只读复用: normalize_code / slice_asof 语义参考)
import engine_v5  # noqa: E402  (主引擎本体, 不修改只调用)


def _slice_rows_asof(bars, asof):
    """对 engine_v5.load_klines 的行列表 [date,o,c,h,l,v] 做 <= asof 截断。
    语义与 _loader.slice_asof 完全一致 (升序, 含 asof 当日)。"""
    out = []
    for b in bars:
        if b[0] <= asof:
            out.append(b)
        else:
            break  # load_klines 已按日期升序排序
    return out


def main():
    # 先轻量扫一遍 argv 拿 --asof, 其余参数原样透传给 engine_v5.main()
    asof = None
    argv = list(sys.argv[1:])
    for i, a in enumerate(argv):
        if a == "--asof" and i + 1 < len(argv):
            asof = argv[i + 1]
            del argv[i:i + 2]
            break
        if a.startswith("--asof="):
            asof = a.split("=", 1)[1]
            del argv[i]
            break

    if asof:
        # 校验格式, 与 _loader.slice_asof 的 YYYY-MM-DD 约定一致
        import re
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", asof):
            print(f"[nolookahead] --asof 格式应为 YYYY-MM-DD, 收到: {asof}",
                  file=sys.stderr)
            return 2

        orig_load_klines = engine_v5.load_klines

        def load_klines_sliced(bulk_dir, limit=None):
            kl = orig_load_klines(bulk_dir, limit)
            n_cut = 0
            for code, bars in kl.items():
                cut = _slice_rows_asof(bars, asof)
                if len(cut) != len(bars):
                    n_cut += 1
                kl[code] = cut
            print(f"[nolookahead] asof={asof}: {n_cut}/{len(kl)} 只 K 线被截断 "
                  f"(语义同 _loader.slice_asof)", flush=True)
            return kl

        engine_v5.load_klines = load_klines_sliced

        # 外部增强数据 (all_sources/moneyflow) 是当前快照, 无历史 → 强制禁用
        if "--no-ext" not in argv:
            argv.append("--no-ext")
            print("[nolookahead] asof 模式强制 --no-ext "
                  "(all_sources/moneyflow 为当前快照, 回测使用即前视)",
                  flush=True)
        print(f"[nolookahead] 无前视模式 asof={asof}, 打分逻辑复用 engine_v5 本体",
              flush=True)
    else:
        print("[nolookahead] 无 --asof, 直通 engine_v5.main() (回归等价)",
              flush=True)

    sys.argv = [os.path.join(SCRIPTS, "engine_v5.py")] + argv
    return engine_v5.main()


if __name__ == "__main__":
    sys.exit(main())
