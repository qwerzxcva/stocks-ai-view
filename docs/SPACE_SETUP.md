# GitCode Space 集群搭建指南

> 目标: 24h 不间断算力, 且单个 Space 休眠不影响整体。
> 用户 2026-10-14 指示: 多开几个组织, 组织里开 Space, 交叉使用。

## 一、为什么需要集群

GitCode Space 的硬限制:

| 限制 | 说明 |
|---|---|
| 休眠 | 30 分钟无 HTTP 访问即休眠 |
| 单组织额度 | 1000 核时/月 |
| Spaces API | **不存在** (已实测 404), 运行地址只能从网页详情页拿 |

单 Space 的问题: 保活靠单一外部 ping,  ping 方自己挂掉就整簇睡着。

## 二、已实现的交叉保活机制

代码在 GitCode `jiegengxiangjianxue/stocks-ai-space` 的 `app.py`:

1. **每个 Space 每 4 分钟 curl 所有同伴的 `/health`** — 只要簇里有一个醒着,
   它就会把其他成员的休眠计时器全部重置
2. **GitHub Actions 每 5 分钟 curl 所有 Space** (full-pipeline 天然是节拍器)
3. **`/health` 返回同伴状态**, 一条 curl 就能判断整簇健康度:
   ```json
   {"status":"ok","heartbeat":"2026-10-14T08:30:00+08:00",
    "peers":{"alive":2,"configured":2},"engines":27,"chain":["valuation_engine",...]}
   ```

## 三、你需要做的事

### 1. 创建组织 B、C

在 GitCode 网页创建两个组织 (名称随意, 下面用 `orgB` / `orgC` 代指),
然后在每个组织里各建一个仓库并启动 Space。

### 2. 三个 Space 的环境变量

| Space | 仓库 | PEER_SPACES (填另外两个的 URL) |
|---|---|---|
| A (已建) | `jiegengxiangjianxue/stocks-ai-space` | `orgB-space-url,orgC-space-url` |
| B (待建) | `orgB/stocks-space` | `orgA-space-url,orgC-space-url` |
| C (待建) | `orgC/stocks-space` | `orgA-space-url,orgB-space-url` |

每个 Space 的启动命令用同一份代码:
```bash
git clone https://gitcode.com/<org>/<repo>.git && cd <repo>
pip install -r requirements.txt
python3 app.py
```

### 3. GitHub Secrets

在 `qwerzxcva/stocks-ai-view` 的 Settings -> Secrets and variables -> Actions 添加:

| Secret | 值 |
|---|---|
| `SPACE_URL_A` | Space A 的运行地址 (形如 `https://xxx-7860.gitcode.run`) |
| `SPACE_URL_B` | Space B 的运行地址 |
| `SPACE_URL_C` | Space C 的运行地址 |
| `GC_TOKEN` | GitCode PAT (需 `read_api` + `write_repository`) |

**URL 从哪拿**: Space 详情页启动后, 页面会显示访问地址, 形如
`https://<something>-7860.gitcode.run`。带不带 `/health` 后缀都可以,
工作流会自动补。

### 4. 分工 (可选优化)

默认三个 Space 都跑同一套 27 引擎 (靠 `git pull` 同步代码, 产出都写
`analysis_out/`)。若要按职责拆分以省核时:

| Space | 引擎 | 环境变量 |
|---|---|---|
| A | 采集 + 全部引擎 | 不设 `ENGINE_ONLY` |
| B | 只跑引擎 | `ENGINE_ONLY=valuation_engine,engine_v5,multidim_engine,...` |
| C | 只跑复盘/AI | `ENGINE_ONLY=reflect_engine,kline_sim,self_reflect,...` |

`ENGINE_ONLY` 未实现的话三个都跑全量也正确, 只是费核时。

## 四、核时账 (决定配几核)

Space 计费 = CPU 核数 x 运行小时数, 休眠不计费。

| 配置 | 24h 常驻月耗 | 单组织 1000 核时 |
|---|---|---|
| 0.5 核 | 360 | 够 (剩 640) |
| 1 核 | 720 | 够 (剩 280) |
| 2 核 | 1440 | 超 44% |

**结论**: 每个 Space 选 1 核或 0.5 核, 单组织额度内富余。

## 五、不配 Space 会怎样

不会坏。`full-pipeline.yml` 里有完整的 22 引擎兜底链, 实测一轮 59 秒,
每 5 分钟跑一次, 数据全部新鲜。Space 只是把重计算从 Actions 的
2000 分钟/月额度里挪到 GitCode 的 1000 核时/月。

## 六、验证

配好后手动触发 `space-pull`, 日志里应看到:

```
Space 保活: 3/3 可达 (本工作流 5 分钟一轮, 天然节拍器)
共拉取 27 个产出文件
```

只看到 `0/3` 说明 URL 填错 (最常见: 填了仓库网页而不是 Space 运行地址)。
