# llm_council.py — 轻量本地 LLM 股票 AI (2026-10-23)
# 角色: 中枢引擎的"第二大脑" — 只陈述与解释, 不改投票权重 (用户定调)
# 模型: Qwen3-0.6B (1.2GB, 4GB RAM 内可跑 int8); 不可用时降级为规则式综合器 (仍可用)
# 用法: llm_council.council(context_dict) -> {'verdict', 'reasoning', 'model', 'degraded'}
import json, os

MODEL_DIR = os.environ.get('QWEN_DIR', '/home/agentuser/models/models/Qwen--Qwen3-0.6B/snapshots/master')
_gen = None

def _load():
    """惰性加载, 全局单例"""
    global _gen
    if _gen is not None:
        return _gen
    try:
        from transformers import pipeline
        # 找 snapshot 子目录
        snap = None
        if os.path.isdir(MODEL_DIR):
            if os.path.isfile(os.path.join(MODEL_DIR, 'config.json')):
                snap = MODEL_DIR
            else:
                import glob as _g
                cands = _g.glob(os.path.join(MODEL_DIR, '*', 'config.json'))
                if cands:
                    snap = os.path.dirname(cands[0])
        if not snap:
            return None
        import glob as _g
        if snap is None:
            cands = _g.glob(os.path.expanduser('/home/agentuser/models/models/Qwen--Qwen3-0.6B/snapshots/*/'))
            snap = cands[0].rstrip('/') if cands else None
        if not snap:
            return None
        _gen = pipeline('text-generation', model=snap, dtype='float32',
                        device_map='cpu', model_kwargs={'low_cpu_mem_usage': True})
        return _gen
    except Exception as e:
        print('llm load fail:', str(e)[:80])
        return None

PROMPT_TMPL = """你是A股投研助手。以下是由量化引擎链产出的候选股与市况数据，请你只做三件事：
1) 逐条陈述每个候选的多空逻辑（不改评分、不给买卖指令）；
2) 指出数据之间的矛盾点（若有）；
3) 给出你自己的置信度(0-100)与一句话总结。
市况: {market}
候选: {picks}
异动: {anomaly}
回答要短，每条不超过两行。
"""

def council(market: dict, picks: list, anomaly: list = None, max_new=380) -> dict:
    prompt = PROMPT_TMPL.format(
        market=json.dumps(market, ensure_ascii=False)[:600],
        picks=json.dumps(picks[:8], ensure_ascii=False)[:1800],
        anomaly=json.dumps(anomaly or [], ensure_ascii=False)[:400])
    gen = _load()
    if gen is None:
        return _degraded(market, picks, anomaly)
    try:
        # chat 模板 + 温度采样 + 反复读惩罚 (0.6B 裸 completion 会复读)
        tok = gen.tokenizer
        msgs = [{'role': 'user', 'content': prompt}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        out = gen(text, max_new_tokens=min(max_new, 200), do_sample=True, temperature=0.7, top_p=0.8,
                  repetition_penalty=1.15, return_full_text=False)[0]['generated_text']
        txt = out.strip()
        # 质量 gate: 复读检测 — 最长重复行占比>40% 视为劣化, 降级规则器
        lines = [l for l in txt.split(chr(10)) if l.strip()]
        if lines and max(lines.count(l) for l in set(lines)) / len(lines) > 0.4:
            r = _degraded(market, picks, anomaly)
            r['degraded_reason'] = 'llm degenerate output'
            return r
        return {'verdict': 'llm', 'reasoning': txt[:1600],
                'model': 'Qwen3-0.6B-local', 'degraded': False}
    except Exception as e:
        r = _degraded(market, picks, anomaly)
        r['degraded_reason'] = str(e)[:100]
        return r

def _degraded(market, picks, anomaly):
    """规则式综合器 (模型不可用时仍保证链路闭环)"""
    lines = []
    for p in picks[:8]:
        s = p.get('score') or p.get('total') or 0
        eng = p.get('engines') or p.get('src') or []
        verdict = '多引擎一致' if len(eng) >= 4 else '证据中等' if len(eng) >= 2 else '证据单薄'
        lines.append(f"{p.get('sym') or p.get('code')}: 综合分{s} {verdict}（{len(eng)}引擎）")
    m = market.get('bias_label') or market.get('phase') or ''
    return {'verdict': 'rules', 'reasoning': f"市况{m}；" + '；'.join(lines)[:1400],
            'model': 'rule-fallback', 'degraded': True}

if __name__ == '__main__':
    r = council({'phase': '震荡市', 'bias_label': '中性'},
                [{'sym': 'sh600900', 'score': 0.72, 'engines': ['a','b','c','d']},
                 {'sym': 'sh601328', 'score': 0.65, 'engines': ['a','b']}])
    print(json.dumps(r, ensure_ascii=False)[:500])
