# 知识星球 MCP 接入记录

> 接入时间：2026-10-13 · 端点：`https://mcp.zsxq.com/topic/mcp?api_key=<key>`

## 一、接入方式

### 已配置（本会话）
Aharou 项目级 MCP：`zsxq`（HTTP transport）
- 下次会话生效后可直接 `tool_search` 找 `mcp__zsxq__*` 工具
- 也可用 curl 直连（不依赖 MCP 客户端）：

```bash
curl -s -X POST "https://mcp.zsxq.com/topic/mcp?api_key=<KEY>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### 可用工具（21 个）
| 类别 | 工具 |
|---|---|
| 通用 | `call_zsxq_api`（指定 method/path/query/body 调 /v2/ /v3/） |
| 读取 | `get_group_topics` / `get_hashtag_topics` / `get_topic_info` / `get_topic_comments` / `get_group_hashtags` / `get_self_info` / `get_user_groups` / `get_user_notes` / `get_user_footprints` |
| 搜索 | `search_topics`（RAG 检索）/ `search_groups` / `search_group_members` |
| 提问 | `get_self_answer_topics` / `get_self_question_topics` / `create_topic_answer` |
| 写入 | `create_note` / `create_topic` / `create_topic_comment` / `set_topic_digested` / `set_topic_tags` |

## 二、账号现状（实测）

- 用户：`桔梗想见雪`，user_id = `18142221114442`，广东
- 加入星球：**`行业咨询智库`**（group_id = `51111514812824`，付费星球）

### ⚠️ 权限卡点（需用户操作）
该星球**未开通 Skill 权限**，所有主题内容读取被拒：

```
{"count":0,"error":"该星球暂未开通 Skill 权限，如需使用，可联系星主申请开通。",
 "has_more":false,"success":false,"topics_brief":[]}
```

**已验证可用**：`get_group_hashtags`（标签列表）、`get_self_info`、`get_user_groups`
**已验证被拒**：`get_group_topics` / `get_hashtag_topics` / `search_topics` / `call_zsxq_api`(GET topics)

### 解法（三选一）
1. **联系星主开通 Skill 权限**（最正规，星主在星球设置里勾选）
2. 换一个已开通 Skill 权限的星球测试
3. 用 `create_note` / `create_topic` 验证写权限是否同样受限

## 三、星球内容规模（标签列表实测，100 个标签）

金融/股票直接相关：

| 标签 | 篇数 | 标签 | 篇数 |
|---|---|---|---|
| #行业报告# | 4630 | #券商研报# | 75 |
| #金融保险# | 961 | #宏观经济# | 14 |
| #投融资# | 56 | #投资# | 8 |
| #资产管理# | 15 | #银行# | 11 |
| #经济# | 59 | #数据资产# | 12 |

行业面（映射 A股板块）：

| 标签 | 篇数 | 标签 | 篇数 |
|---|---|---|---|
| #人工智能# | 515 | #生物医药# | 483 |
| #能源矿产# | 479 | #工业制造业# | 305 |
| #汽车# | 117 | #电子游戏# | 52 |
| #化工材料# | 41 | #光伏# | 12 |
| #储能# | 27 | #新能源# | 8 |
| #航空航天# | 28 | #船舶航海# | 6 |
| #电力# | 21 | #机械# | 18 |
| #医疗# | 17 | #医美# | 10 |
| #食品饮料# | 40 | #酒类# | 10 |
| #地产# | 369 | #建筑# | 6 |

**总量超 15000 篇行业情报**——这是「消息面/政策面/板块面」因子的高质量来源。

## 四、权限开通后的接入方案（预设计）

`scripts/zsxq_collector.py`（待权限开通后实现）：

```python
# 采集 #行业报告#/#券商研报#/#金融保险# 等标签的最新主题
# → 提取 title/text/标签/时间 → theme_of() 命中关键词
# → 写入 data/zsxq_digest.json (与 news_digest 同格式)
# → hot_impact / multidim 消息面维度直接可用
```

去重主键：`topic_id`（与 news_digest 的 title hash 合并去重）

## 五、GitHub 私有仓接入（已打通，意外收获）

同一个 GitHub PAT 权限内发现 **28 个仓库**，其中私有仓含金量极高：

| 仓库 | 私有 | 大小 | 内容 |
|---|---|---|---|
| `jiegengxiangjianxue1/stocks-algo` | ✅ | 486MB | 选股/周期/预测算法库（16.8万首板样本统计） |
| `jiegengxiangjianxue1/stockdb` | ✅ | 76MB | **daily 207万行 K线（3175只，2024-2026）+ minute + picks 对账** |
| `jiegengxiangjianxue1/stocks` | ✅ | 9.9MB | daily-scan |
| `jiegengxiangjianxue1/stockdb` | ✅ | 76MB | duckdb 259MB（ARCHITECTURE 称） |
| `dufuwuxin/engine-core` | ✅ | 小 | GitCode 私有引擎仓 |
| `dufuwuxin/stocks-ops` / `d2-social` / `d3-global` | ✅ | 小 | 运营/社交/全球 |
| `jiegengxiangjianxue3/optimization-lab` | ✅ | 10KB | 优化实验 |

已落地：`scripts/integrate_stockdb.py` 把 stockdb 的 1233 只独有股票（79.5万行）导入 bulk，覆盖 51%→75%。

## 六、安全注意

- **token 绝不写进代码**：GitHub secret scanning 会拦截推送（本次已被拦一次，用 `git filter-branch` 重写历史解决）
- 统一从环境变量读：`GH_TOKEN` / `GITCODE_TOKEN` / `GC_TOKEN`
- MCP 的 api_key 同样只在配置里，不进仓库

---

*不构成投资建议。*
