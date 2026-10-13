# 新数据源探测结论 (2026-10-13)

## 一、总览

| # | 来源 | URL | 可达性 | 内容类型 | 建议接入 |
|---|------|-----|--------|---------|---------|
| 1 | 抖音热榜 | `https://aweme.snssdk.com/aweme/v1/hot/search/list/` | ✅ 200 | JSON（公开无鉴权） | 可直接 HTTP 调用 |
| 2 | 小红书热话题 | `https://www.xiaohongshu.com/explore` / `edith.xiaohongshu.com/api/sns/web/v1/...` | ❌ 404/403 | SPA（React） | 需浏览器渲染 / 无公开 API |
| 3 | 腾讯新闻作者页 | `https://news.qq.com/omn/author/5233565` | ⚠️ HTML 200 | React SPA（仅 shell） | 需找对应 API；作者页 JS 内嵌 `channelInfo` 含作者元数据 |
| 4 | 东方财富基金扩展字段 | `https://fund.eastmoney.com/pingzhongdata/{code}.js?v={ts}` | ✅ 200 | JS 脚本（可解析） | **推荐方案**：直接取 JS 变量 |
| 5 | 雪球热门股/讨论 | `https://xueqiu.com/v5/statuses/hot/stock_list.json` | ❌ 404/302 | SPA + 反爬 | 需 cookie (`xq_a_token`)；无公开接口 |

---

## 二、各源详细结论

### 1. 抖音热搜 API ✅

- **接口地址**: `https://aweme.snssdk.com/aweme/v1/hot/search/list/`
- **请求方式**: GET（无参数）
- **鉴权**: 无需 cookie / token（公开接口）
- **Content-Type**: `application/json`
- **User-Agent 要求**: 建议使用移动端 UA（iPhone/Android），桌面 UA 偶尔返回不同数据
- **Referer**: `https://www.douyin.com/`（可选，不带也能返回数据）

**返回字段结构**:

```json
{
  "status_code": 0,
  "data": {
    "active_time": "2026-10-13 16:09:05",
    "trending_desc": "实时上升热点",
    "trending_list": [
      {
        "word": "C罗凌空抽射斩获生涯第981球",
        "hot_value": 0,
        "rank": null,
        "video_count": 5,
        "discuss_video_count": 1,
        "sentence_id": "2692404",
        "sentence_tag": 5000,
        "word_type": 0,
        "label": 0,
        "display_style": 0,
        "group_id": "7695108382508307747",
        "event_time": 1791846081,
        "word_cover": { "uri": "...", "url_list": [...] },
        "related_words": null,
        "article_detail_count": 0,
        "aweme_infos": null,
        "hotlist_param": "{\"version\":1}",
        "word_sub_board": null,
        "drift_info": null
      }
    ],
    "share_info": {
      "share_title": "...",
      "share_link_desc": "...",
      "share_url": "https://www.iesdouyin.com/share/billboard/?id=0"
    },
    "word_list": [...],
    "recommend_list": null
  },
  "banner_dark": {...},
  "banner_light": {...},
  "ad_search_list": null,
  "board_tabs": null
}
```

**可用字段**:
- `data.trending_list[].word` — 热搜词（核心）
- `data.trending_list[].hot_value` — 热度值（当前为 0，可能需特定 UA/地域才返回实际值）
- `data.trending_list[].video_count` — 相关视频数
- `data.trending_list[].discuss_video_count` — 讨论视频数
- `data.trending_list[].sentence_id` — 句子 ID（可用于去重/追踪）
- `data.active_time` — 数据更新时间
- `data.share_info.share_url` — 分享链接

**备注**: `hot_value` 在本次探测中均为 0，可能是测试环境或接口改版导致。`sentence_id` 是唯一稳定标识符，可用于增量拉取。

**候选替代接口**:
- `https://aweme.snssdk.com/aweme/v1/discover/search/hot/` → 返回 `{has_more:0, cursor:10, category_list:[]}`，无热词数据，不可用。
- `https://aweme.snssdk.com/aweme/v1/web/discover/search/suggest/` → `status_code:1 "Url does not match"`，路径错误。

**建议接入方式**: 直接调用 `/aweme/v1/hot/search/list/`，以 `sentence_id` 做增量去重；`hot_value` 为 0 时暂用 `video_count` 作为排序依据。

---

### 2. 小红书搜索/热门话题 ❌

- **主站**: `https://www.xiaohongshu.com/explore`（React SPA，JS 动态渲染）
- **猜测 API 路径**:
  - `https://edith.xiaohongshu.com/api/sns/web/v1/search/hot` → **404**
  - `https://edith.xiaohongshu.com/api/sns/web/v1/search/keyword?keyword=热门` → **404**
  - `https://edith.xiaohongshu.com/api/sns/web/v1/feed?source=note_search_keywords&keyword=热门` → **404**

**结论**: 小红书所有可猜测的公开 API 路径均返回 404，说明接口已收紧或路径变更。页面为完整 SPA，内容需浏览器渲染。

**建议接入方式**: 暂不可接入 HTTP 抓取。如需数据，只能通过：
1. 内置浏览器（Playwright/Puppeteer）渲染后解析 DOM；
2. 逆向分析 app 端抓包获取真实接口（需设备 root 权限 + mitmproxy）；
3. 使用第三方数据服务（如新榜、蝉妈妈等付费 API）。

---

### 3. 腾讯新闻作者页 ⚠️

- **URL**: `https://news.qq.com/omn/author/5233565`
- **HTTP 200**，返回 React SPA shell（HTML 含 `<div id="root">`）
- **HTML 内嵌数据**:
  ```js
  window.channelInfo = {
    "IS_AUTHOR_PAGE": true,
    "channelKey": "5233565",
    "channelType": "author",
    "channelConfig": {
      "channelNav": [ /* 导航菜单 */ ]
    }
  };
  ```

**探测的 API 候选（全部失败）**:
- `GET https://i.news.qq.com/web_backend/GetNewsList?channel_id=5233565&channel_type=author&offset=0&limit=20` → **404**
- `GET https://i.news.qq.com/trpc.qqnews_web.kv_srv/kv_srv/KvSrv.GetAuthorContentList?reqdata={...}` → **404**
- `GET https://i.news.qq.com/trpc.qqnews_web.page_srv/PageSrv.GetChannelInfo?reqdata={...}` → **404**

**结论**: 作者页是 React SPA，文章列表由客户端 JS 通过私有 trpc 接口异步加载，无公开 REST API 可用。channel.js（3.4MB bundle）中只识别出 `/gw/type/subList` 和 `getWebPacUid` 两个同域路径，无作者内容列表接口。

**建议接入方式**:
1. **最佳方案**: 使用已内置的 Chrome/Puppeteer 容器渲染页面后爬取 DOM；
2. **备选方案**: 从 HTML 中提取 `window.channelInfo` 拿作者元数据（`channelKey`、`channelType`、导航菜单）；
3. **不推荐**: 逆向 trpc 接口（需抓包 APP 端或分析加密参数）。

---

### 4. 东方财富基金扩展字段 ✅

#### 方案 A：pingzhongdata JS 接口（✅ 推荐）

- **接口地址**: `https://fund.eastmoney.com/pingzhongdata/{基金代码}.js?v={时间戳}`
- **请求方式**: GET
- **返回格式**: JavaScript 脚本（非 JSON），可直接解析变量
- **HTTP 200**，无需鉴权

**解析字段示例** (基金代码 `000001`):
```javascript
var fS_name = "华夏成长混合";       // 基金名称
var fS_code = "000001";             // 基金代码
var fund_sourceRate = "1.50";       // 原申购费率
var fund_Rate = "0.15";             // 现申购费率（打折后）
var fund_minsg = "10";              // 最小申购金额
var stockCodes = ["3003080","6883471", ...];  // 持仓股票代码
// 以及 ishb、其他持仓字段等
```

**可用字段**:
- `fS_name` — 基金名称
- `fS_code` — 基金代码
- `fund_Rate` / `fund_sourceRate` — 现费率 / 原费率
- `fund_minsg` — 最低申购金额
- `stockCodes` — 前十大重仓股票代码数组
- `ishb` — 是否沪深港通标的（true/false）

**备注**: 该接口依赖 `v` 参数控制缓存，建议每次请求使用当前时间戳（如 `20261013161200`）。

#### 方案 B：FundJZ/FundGS .aspx 接口（❌ 不可用）

- `https://fund.eastmoney.com/Data/Fund_Dataservice_FundJZ.aspx?code=000001&d=2026-10-12` → **403 Forbidden**（WAF 拦截）
- `https://fund.eastmoney.com/Data/Fund_Dataservice_FundGS.aspx?code=000001` → **403 Forbidden**
- 即使加 `Referer: https://fund.eastmoney.com/` 仍返回 403，说明本地容器出口被 WAF 识别

**结论**: .aspx 数据接口因 WAF 防护本地不可用（与项目记忆 `data_source_probe_conclusions` 中"东财 WAF 降级"一致，GitHub Actions 出口通常全通）。推荐使用 **方案 A**（pingzhongdata JS）。

---

### 5. 雪球热门股/讨论 ❌

- **主站**: `https://xueqiu.com`（强反爬 SPA）
- **探测接口**:
  - `GET /v5/stock/hot/list.json?page=1&size=20&type=20` → **HTTP 400 "无状态描述"**
  - `GET /v5/statuses/hot/stock_list.json?source=homepage_new&type=20` → **302 重定向 + anti-bot JS**
  - `GET /statuses/search/show.json?query=000001&sort=time&type=8` → **HTTP 400 "请刷新页面或重新登录"**
  - `GET /snowx/S/SH000001/detail` → **302 + anti-bot JS**

**结论**: 雪球所有公开端点均需有效 `xq_a_token` cookie 认证，无 cookie 时返回 400/302 拦截页面。无法零凭据 HTTP 抓取。

**建议接入方式**:
1. 使用已有用户的 `xq_a_token` cookie（需手动从浏览器复制）；
2. 通过 Playwright 等浏览器自动化工具保持 session；
3. 考虑替代数据源（如东方财富股吧、同花顺热帖等）。

---

## 三、接入优先级建议

| 优先级 | 来源 | 理由 |
|--------|------|------|
| P0 | 抖音热榜 API | 公开接口，零凭据，JSON 结构化，可直接集成 |
| P0 | 东方财富 pingzhongdata JS | 公开接口，返回基金扩展字段（持仓代码/费率等），解析稳定 |
| P1 | 腾讯新闻作者页 | 可从 HTML 提取 `channelInfo` 元数据；文章列表需浏览器渲染 |
| P2 | 小红书 | 需浏览器自动化或第三方数据源，成本高 |
| P2 | 雪球热门股 | 需 cookie 认证，无公开接口，替代方案多 |

---

## 四、实现建议

### 抖音热榜
```python
import requests

def fetch_douyin_hot():
    resp = requests.get(
        "https://aweme.snssdk.com/aweme/v1/hot/search/list/",
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Referer": "https://www.douyin.com/",
        },
        timeout=15,
    )
    data = resp.json()["data"]
    items = data.get("trending_list", [])
    return [
        {"word": it["word"], "sentence_id": it["sentence_id"],
         "video_count": it["video_count"],
         "active_time": data["active_time"]}
        for it in items
    ]
```

### 东方财富基金字段
```python
import re
from ast import literal_eval

def fetch_fund_meta(code: str) -> dict:
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js?v={int(__import__('time').time()*1000)}"
    resp = requests.get(url, timeout=10)
    text = resp.text
    vars = {}
    for match in re.finditer(r'var\s+(\w+)\s*=\s*([^;]+);', text):
        name, value = match.group(1), match.group(2).strip()
        try:
            vars[name] = literal_eval(value)  # 仅解析安全字面量，避免 eval 风险
        except Exception:
            vars[name] = value
    return vars  # 含 fS_name, fS_code, fund_Rate, stockCodes 等
```

### 腾讯新闻作者元数据
```python
def fetch_qq_author_meta(author_id: str) -> dict:
    """从 HTML 提取 channelInfo"""
    url = f"https://news.qq.com/omn/author/{author_id}"
    resp = requests.get(url, headers={"User-Agent": ...}, timeout=10)
    import re, json
    m = re.search(r'window\.channelInfo\s*=\s*({[^;]+});', resp.text)
    if m:
        return json.loads(m.group(1))
    return {}
```

---

*生成时间: 2026-10-13 16:12 (UTC+8)*  
*工具: webfetch + curl (移动端 UA)*  
*参考: 项目记忆 `data_source_probe_conclusions`*
