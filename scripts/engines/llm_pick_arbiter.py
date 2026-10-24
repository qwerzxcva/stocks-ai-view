#!/usr/bin/env python3
"""
llm_pick_arbiter.py — LLM 调分裁决器 (中枢终审候选 × LLM 调分, 只调分不选股)

定位 (硬契约):
  - 裁决权在中枢: central_orchestrator 调用本文件, 本文件只对其给的候选做 adjust
    调分, 绝不新增/删除候选, 绝不直接产出推股名单。
  - LLM 输出契约 (参考 jadesun/ai_stock 的 {推荐,置信度,风险} 契约, Apache-2.0,
    独立实现): {signal, confidence, reasoning, adjust}, adjust ∈ [-max, +max] 封顶截断。

双层模型策略 (用户拍板):
  - 主链路 (选股/分析/推股/复盘/分析总结): 重推理分析模型 (--mode heavy)。
  - 辅助模块 (异动/热点/另类因子): 轻推理分析模型 (--mode light)。
  - --mode auto: 中枢批量终审默认走 heavy 探测链, 探测不到逐级降级。

Provider 探测链 (全部可选, 凭据只从环境变量读, 绝不打印, 缺失自动降级):
  heavy 链: GitCode AI (GITCODE_TOKEN, DeepSeek-V4-Flash 主/Qwen3-30B 备)
            → GitHub Models (GITHUB_TOKEN/GH_TOKEN, openai/gpt-4o 高阶)
            → OpenAI 兼容 (OPENAI_API_KEY)
  light 链: ghc CLI (GitHub Models llama-3.1-8b-instant, 探测 which ghc)
            → Ollama 本地 (探测 localhost:11434/api/tags)
            → GitHub Models (GITHUB_TOKEN, llama-3.1-8b-instant)
  全部不可用 → --fallback rule: 基于输入维度分的启发式 adjust,
               输出标注 degraded=true, llm=unavailable。规则降级路径必须完整可用。

开源选型路线 (用户裁决「两者并行」, 均为独立实现声明):
  - FinGPT 路线: 借鉴其「特征/文本→指令模板→标签+置信度」推理管线 (MIT, 自训
    LoRA 属后续方向); 来源 https://github.com/AI4Finance-Foundation/FinGPT (MIT)
  - qlib model zoo 路线: 量化信号 baseline; 来源 https://github.com/microsoft/qlib (MIT)
  - 架构概念参考 heyfluke/stock-scanner (无 LICENSE, 仅借鉴公开架构概念——多视角
    评审思路, 独立实现, 未复制任何代码)
  - jadesun/ai_stock (Apache-2.0): 「新闻→LLM→{推荐,置信度,风险}」输出契约参考。

用法:
  python3 llm_pick_arbiter.py --input candidates.json --mode auto --out arbiter.json
  cat candidates.json | python3 llm_pick_arbiter.py --input - --fallback rule
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "llm_pick_arbiter.json"))


CST = timezone(timedelta(hours=8))
ENGINE = "llm_pick_arbiter"

# ── Provider 端点/模型 (与 scripts/gitcode_ai_review.py, scripts/ai_commentary.py 同约定) ──
GITCODE_ENDPOINT = "https://gitcode.com/api/v5/chat/completions"
GITCODE_HEAVY_MODELS = ["deepseek-ai/DeepSeek-V4-Flash", "Qwen/Qwen3-30B-A3B-Instruct-2507"]
GH_MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
GH_HEAVY_MODEL = "openai/gpt-4o"
GH_LIGHT_MODEL = "meta-llama/llama-3.1-8b-instant"
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
OPENAI_HEAVY_MODEL = "gpt-4o"
OLLAMA_URL = "http://localhost:11434"

REASONING_MAX = 500  # reasoning 截断上限 (字)


# ══ Prompt 模板 (内置于文件) ══
# FinGPT 管线思路: 结构化特征+文本 → 指令模板 → {标签(signal), 置信度, 幅度}
def heavy_prompt(cands, news_text):
    """重推理模板: 多视角评审 (趋势/价位/风险/反向论证/综合)。
    多角色概念参考 stock-scanner 公开架构, 独立实现。"""
    return (
        "你是A股终审委员会, 对候选股做多视角评审。请依次用五个视角分析后综合:\n"
        "1) 趋势视角: 技术面/资金/情绪是否共振;\n"
        "2) 价位视角: 入场区/止损/目标位风险收益比是否合理;\n"
        "3) 风险视角: 高位/退潮/regime 不匹配等风险;\n"
        "4) 反向论证: 主动寻找这只票不该买的理由, 评估其说服力;\n"
        "5) 综合: 加权得出最终调分。\n"
        "对每只股票输出一行 JSON (不要输出任何其他文字, 不要代码块):\n"
        '{"code":"代码","signal":"bull|bear|neutral","confidence":0.0-1.0,'
        '"reasoning":"<=200字依据(必须引用给定数据)","adjust":整数,-10到+10}\n'
        "adjust 表示对原综合分的调整幅度: 看多且有数据支持为正, 看空为负, 不确定为0。\n"
        "候选 (JSON):\n" + json.dumps(cands, ensure_ascii=False) +
        (("\n消息面:\n" + news_text[:1500]) if news_text else "")
    )


def light_prompt(cands, news_text):
    """轻推理模板: 单视角快评 (辅助模块用, 低延迟低成本)。"""
    return (
        "你是A股快评助手。对每只候选股只看技术面/情绪/regime 三项做一句话判断,\n"
        "每行输出一个 JSON (不要其他文字):\n"
        '{"code":"代码","signal":"bull|bear|neutral","confidence":0.0-1.0,'
        '"reasoning":"<=80字","adjust":整数,-10到+10}\n'
        "候选 (JSON):\n" + json.dumps(cands, ensure_ascii=False) +
        (("\n消息面:\n" + news_text[:800]) if news_text else "")
    )


# ══ Provider 调用 (独立实现, 不 import 仓库其它脚本以免副作用) ══
def _chat(endpoint, model, prompt, token, extra_headers=None, timeout=120):
    """OpenAI chat/completions 兼容调用。失败返回 None。"""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content":
             "你是A股量化评审模型。只输出每行一个JSON, 不输出任何解释、markdown或代码块。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }).encode()
    headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
            return (d.get("choices") or [{}])[0].get("message", {}).get("content")
    except urllib.error.HTTPError as e:
        print("[arbiter] %s HTTP %s" % (model, e.code), file=sys.stderr)
    except Exception as e:
        print("[arbiter] %s %s: %s" % (model, type(e).__name__, str(e)[:100]), file=sys.stderr)
    return None


def call_gitcode(prompt):
    token = os.environ.get("GITCODE_TOKEN", "")
    if not token:
        return None, None
    for m in GITCODE_HEAVY_MODELS:
        c = _chat(GITCODE_ENDPOINT, m, prompt, token, timeout=180)
        if c:
            return "gitcode:%s" % m, c
    return None, None


def call_github_models(prompt, model):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return None, None
    c = _chat(GH_MODELS_ENDPOINT, model, prompt, token,
              {"Accept": "application/vnd.github+json"})
    return ("ghmodels:%s" % model, c) if c else (None, None)


def call_openai(prompt):
    token = os.environ.get("OPENAI_API_KEY", "")
    if not token:
        return None, None
    base = os.environ.get("OPENAI_BASE_URL", OPENAI_ENDPOINT).rstrip("/")
    if not base.endswith("chat/completions"):
        base += "/chat/completions"
    c = _chat(base, OPENAI_HEAVY_MODEL, prompt, token)
    return ("openai:%s" % OPENAI_HEAVY_MODEL, c) if c else (None, None)


def call_ghc_cli(prompt, model=GH_LIGHT_MODEL):
    """ghc CLI 通道 (探测 which ghc; 参考 scripts/llm_review.py 的调用方式, 独立实现)。"""
    if not shutil.which("ghc"):
        return None, None
    try:
        r = subprocess.run(["ghc", "model", "run", "--model", model, "--size", "large",
                            "--prompt", prompt],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and r.stdout.strip():
            return "ghc:%s" % model, r.stdout
        print("[arbiter] ghc rc=%s" % r.returncode, file=sys.stderr)
    except Exception as e:
        print("[arbiter] ghc %s: %s" % (type(e).__name__, str(e)[:100]), file=sys.stderr)
    return None, None


def _ollama_alive():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def call_ollama(prompt):
    """本地 Ollama (探测 localhost:11434/api/tags, 取第一个可用小模型)。"""
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as r:
            tags = json.loads(r.read().decode())
        models = [m.get("name") for m in (tags.get("models") or []) if m.get("name")]
        if not models:
            return None, None
        model = models[0]
        body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            c = json.loads(r.read().decode()).get("response", "").strip()
        return ("ollama:%s" % model, c) if c else (None, None)
    except Exception:
        return None, None


def probe_chain(mode):
    """按模式返回有序 (名称, 调用函数) 探测链。"""
    heavy = [("gitcode_heavy", call_gitcode),
             ("ghmodels_heavy", lambda p: call_github_models(p, GH_HEAVY_MODEL)),
             ("openai_heavy", call_openai)]
    light = [("ghc_light", call_ghc_cli),
             ("ollama_light", call_ollama),
             ("ghmodels_light", lambda p: call_github_models(p, GH_LIGHT_MODEL))]
    if mode == "heavy":
        return heavy + light
    if mode == "light":
        return light + heavy
    return heavy + light  # auto: 主链路默认重推理优先


def probe_availability():
    """探测各通道是否「有凭据/可连接」 (不真正调用, 不打印 key)。"""
    return {
        "gitcode (GITCODE_TOKEN)": bool(os.environ.get("GITCODE_TOKEN")),
        "ghmodels (GITHUB_TOKEN/GH_TOKEN)": bool(
            os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")),
        "openai (OPENAI_API_KEY)": bool(os.environ.get("OPENAI_API_KEY")),
        "ghc CLI": bool(shutil.which("ghc")),
        "ollama (localhost:11434)": _ollama_alive(),
    }


# ══ LLM 输出容错解析 ══
def parse_llm_picks(text):
    """逐行提取 JSON 对象; 容忍 markdown 代码块/前后噪文; 全失败返回 None。"""
    picks = {}
    for line in (text or "").splitlines():
        line = line.strip().strip("`")
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("code"):
            picks[str(d["code"])] = d
    return picks or None


def sanitize(code, raw, max_adjust):
    """清洗单条 LLM 输出: adjust 封顶截断并记录, reasoning 截断, 字段兜底。"""
    notes = []
    try:
        adj = int(float(raw.get("adjust", 0)))
    except (TypeError, ValueError):
        adj, notes = 0, ["adjust非数值, 归零"]
    if abs(adj) > max_adjust:
        clipped = max(-max_adjust, min(max_adjust, adj))
        notes.append("adjust %s 超界, 截断为 %+d" % (raw.get("adjust"), clipped))
        adj = clipped
    sig = str(raw.get("signal", "neutral")).lower()
    if sig not in ("bull", "bear", "neutral"):
        sig, notes = "neutral", notes + ["signal非法, 置neutral"]
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    reasoning_full = str(raw.get("reasoning", ""))
    reasoning = reasoning_full[:REASONING_MAX]
    if len(reasoning_full) > REASONING_MAX:
        notes.append("reasoning 截断至 %d 字" % REASONING_MAX)
    return {"signal": sig, "confidence": round(conf, 3), "reasoning": reasoning,
            "adjust": adj, "notes": notes}


# ══ rule 降级: 基于输入维度分的启发式 adjust ══
DIM_KEYS = ["技术面", "资金面", "高低位", "基本面", "板块面",
            "市场面", "情绪面", "周期面", "消息面", "海外面"]


def rule_adjust(cand, max_adjust):
    """无 LLM 时的确定性降级: 维度分离散度 + regime 匹配 → 保守微调。
    原则: 维度均衡 (标准差小) 且均值高 → 小幅上调; 严重偏科/高位偏热 → 下调。"""
    dims = cand.get("dims") or {}
    vals = [float(dims[k]) for k in DIM_KEYS
            if isinstance(dims, dict) and isinstance(dims.get(k), (int, float))]
    reasons, adj = [], 0
    if vals:
        mean = sum(vals) / len(vals)
        sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        if sd <= 8 and mean >= 60:
            adj += 2
            reasons.append("维度均衡(sd %.1f, 均值 %.0f)" % (sd, mean))
        elif sd >= 20:
            adj -= 2
            reasons.append("维度偏科(sd %.1f)" % sd)
        if isinstance(dims.get("高低位"), (int, float)) and float(dims["高低位"]) >= 85:
            adj -= 2
            reasons.append("高低位 %.0f 偏热" % float(dims["高低位"]))
    regime = str(cand.get("regime") or "").lower()
    S = cand.get("S")
    if regime == "bear" and isinstance(S, (int, float)) and S >= 80:
        adj -= 2
        reasons.append("bear 市高分票, 防派发")
    elif regime == "bull" and isinstance(S, (int, float)) and S >= 75:
        adj += 1
        reasons.append("bull 市顺势")
    adj = max(-max_adjust, min(max_adjust, adj))
    return {"signal": "bull" if adj > 0 else ("bear" if adj < 0 else "neutral"),
            "confidence": 0.4 if vals else 0.3,
            "reasoning": "[rule降级] " + ("; ".join(reasons) if reasons else "无显著触发项, 保守调0"),
            "adjust": adj, "notes": ["rule_fallback"]}


# ══ 主流程 ══
def normalize_input(data):
    """输入契约: {top30:[{code, S, dims/维度分, price/价位, regime}], news_text?}"""
    cands = data.get("top30") or data.get("candidates") or []
    out = []
    for c in cands:
        out.append({
            "code": str(c.get("code", "")),
            "S": c.get("S") if c.get("S") is not None else c.get("score"),
            "dims": c.get("dims") or c.get("维度分") or {},
            "price": c.get("price") or c.get("价位") or {},
            "regime": c.get("regime"),
        })
    return out, (data.get("news_text") or data.get("消息面") or "")


def main():
    ap = argparse.ArgumentParser(
        description="LLM 调分裁决器: 只调分不选股, 裁决权在中枢; 无凭据自动 rule 降级")
    ap.add_argument("--input", required=True, help="中枢终审候选 JSON 文件路径, 或 '-' 读 stdin")
    ap.add_argument("--mode", choices=["heavy", "light", "auto"], default="auto",
                    help="heavy=重推理(主链路) light=轻推理(辅助) auto=重优先逐级降级")
    ap.add_argument("--max-adjust", type=int, default=10, help="adjust 幅度封顶 (默认10)")
    ap.add_argument("--fallback", choices=["rule"], default="rule",
                    help="全部 provider 不可用时的降级策略")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 路径; 缺省打印 stdout ('--out -' 输出 stdout)")
    ap.add_argument("--probe-only", action="store_true", help="只探测通道可用性后退出")
    args = ap.parse_args()

    availability = probe_availability()
    if args.probe_only:
        print(json.dumps(availability, ensure_ascii=False, indent=2))
        return 0

    if args.input == "-":
        data = json.load(sys.stdin)
    else:
        data = json.load(open(args.input, encoding="utf-8"))
    cands, news_text = normalize_input(data)
    if not cands:
        print("[arbiter] 输入无候选 (top30 为空)", file=sys.stderr)
        return 1

    # ── 探测链逐级尝试 ──
    prompt_fn = heavy_prompt if args.mode in ("heavy", "auto") else light_prompt
    model_used, content, tried = None, None, []
    for name, fn in probe_chain(args.mode):
        tried.append(name)
        model_used, content = fn(prompt_fn(cands, news_text))
        if content:
            break

    # ── 解析或降级 ──
    degraded, llm_map = False, None
    if content:
        llm_map = parse_llm_picks(content)
        if llm_map is None:
            print("[arbiter] LLM 返回非JSON, 全量 adjust=0 降级", file=sys.stderr)
            degraded = True
    else:
        degraded = True

    picks = []
    for c in cands:
        raw = (llm_map or {}).get(c["code"])
        if raw is not None:
            s = sanitize(c["code"], raw, args.max_adjust)
        elif degraded and not content:
            s = rule_adjust(c, args.max_adjust)  # 无 provider: 规则启发式
        else:
            # provider 可用但该 code 未被 LLM 返回/解析失败: 保守归零
            s = {"signal": "neutral", "confidence": 0.0,
                 "reasoning": "[降级] LLM 未覆盖该候选或解析失败, adjust=0",
                 "adjust": 0, "notes": ["llm_parse_miss"]}
        prev_S = c.get("S")
        new_S = round(prev_S + s["adjust"], 2) if isinstance(prev_S, (int, float)) else None
        picks.append({"code": c["code"], "prev_S": prev_S, "adjust": s["adjust"],
                      "new_S": new_S, "signal": s["signal"],
                      "confidence": s["confidence"], "reasoning": s["reasoning"],
                      **({"notes": s["notes"]} if s["notes"] else {})})

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": ENGINE,
        "model_chain": {
            "requested": args.mode,
            "actual": model_used or "rule_fallback",
            "degraded": degraded or (model_used is None),
            "tried": tried,
            "provider_availability": availability,
        },
        "picks": picks,
        "data_asof": data.get("date") or data.get("asof") or data.get("data_asof"),
        "meta": {
            "n_candidates": len(picks),
            "max_adjust": args.max_adjust,
            "contract": "只调分不选股; 裁决权在 central_orchestrator",
            "caveat": "LLM/规则调分仅供研究参考, 不构成投资建议",
        },
    }
    txt = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out and args.out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print("[arbiter] → %s (actual=%s degraded=%s)" % (
            args.out, out["model_chain"]["actual"], out["model_chain"]["degraded"]),
            file=sys.stderr)
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
