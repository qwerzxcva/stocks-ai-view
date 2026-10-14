import hashlib


def cls_sign(params):
    """财联社 v1 签名 — 逆向自 wwwjs.cls.cn/_next/static/chunks/pages/_app JS

    算法 (模块 85073 request 封装 + 模块 36498):
      1. 参数补默认值: os=web, sv=8.7.9, app=CailianpressWeb
      2. 按 key 字典序排序 (JS 的 sort() 默认按字符串)
      3. 每项格式化为 key=value; 数组是 key[0]=v&key[1]=v; 嵌套 object 递归
      4. filter 掉空值, 用 & 连接
      5. sign = md5(上面的串).hexdigest()  (小写)
    无需 secret, 无需登录态。
    """
    p = dict(params or {})
    p.setdefault("os", "web")
    p.setdefault("sv", "8.7.9")
    p.setdefault("app", "CailianpressWeb")

    def fmt(key, val):
        if val is None:
            return ""
        t = type(val)
        if t is str or t is int or t is float or t is bool:
            return "%s=%s" % (key, val)
        if isinstance(val, (list, tuple)):
            parts = [fmt("%s[%d]" % (key, i), v) for i, v in enumerate(val)]
            return "&".join(x for x in parts if x)
        if isinstance(val, dict):
            parts = []
            for k in sorted(val.keys()):
                s = fmt("%s[%s]" % (key, k), val[k])
                if s:
                    parts.append(s)
            return "&".join(parts)
        return ""

    pairs = []
    for k in sorted(p.keys()):
        s = fmt(k, p[k])
        if s:
            pairs.append(s)
    raw = "&".join(pairs)
    # 模块 36498 的 sign: i(t = n.sync(t))
    #   n.sync = 模块 37371 = WebCrypto **sha-1** (同步版)
    #   i      = 模块 81383 = **md5** (常量表 0xd76aa478/0xe8c7b756 是 MD5 T-table,
    #            SHA-1 只有 4 个常量; 输出 bytesToHex)
    # 所以 sign = md5( sha1(params_str) )  — 两层 hash
    return hashlib.md5(hashlib.sha1(raw.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest(), raw


if __name__ == "__main__":
    # 自测: 与 JS 行为对齐的样例
    sign, raw = cls_sign({"refresh_type": 1, "rn": 20})
    print("raw :", raw)
    print("sign:", sign)
