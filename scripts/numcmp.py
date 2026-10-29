"""numcmp.py — 数值带容差的深比较（闸门共用的比较原语）。

## 为什么需要它

2026-10-29 CI 实测：本地用 Python 3.14 生成黄金向量、CI 用 3.11 校验，
`json.dumps(a) == json.dumps(b)` 这种字符串全等把 vol_target 判成「漂移」——
两边的数其实一样，只是浮点 repr 不同。黄金向量自己的契约写的是
「strength ±0.001 / score ≤0.05」，说明它想要的是容差比较，不是字节相等。

这里提供 `deep_diff(a, b)`：一致返回 None，不一致返回第一处路径说明
（如 `vol_target.cases[0].expect.scale 数值不同: 1.01 vs 1.0`），
让闸门报错能直接指到哪一条用例的哪个字段。
"""

DEFAULT_TOL = 1e-9


def deep_diff(a, b, path="", tol=DEFAULT_TOL):
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return "%s 键不同: %s vs %s" % (path or "<root>", sorted(a.keys()), sorted(b.keys()))
        for k in a:
            d = deep_diff(a[k], b[k], "%s.%s" % (path, k), tol)
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return "%s 长度不同: %d vs %d" % (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = deep_diff(x, y, "%s[%d]" % (path, i), tol)
            if d:
                return d
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a is b else "%s 布尔不同: %r vs %r" % (path, a, b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if abs(a - b) <= tol * max(1.0, abs(a), abs(b)):
            return None
        return "%s 数值不同: %r vs %r" % (path, a, b)
    return None if a == b else "%s 不同: %r vs %r" % (path, a, b)
