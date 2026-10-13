#!/usr/bin/env python3
"""
deploy_cloud.sh — 云服务器 (GitCode Notebook / 独立服务器) 一键部署

用法 (在云服务器上执行):
  curl -fsSL https://raw.githubusercontent.com/qwerzxcva/stocks-ai-view/main/scripts/deploy_cloud.sh | bash
  或:
  git clone https://github.com/qwerzxcva/stocks-ai-view.git && cd stocks-ai-view && bash scripts/deploy_cloud.sh

做什么:
  1. 装依赖 (python3 + duckdb + akshare)
  2. 装 systemd 服务 (分钟级采集, 常驻)
  3. 首次全量回补 (复权 + 指数 + 周期池)
  4. 启动实时采集 (交易时段每 60 秒)
  5. 配置 git 推送 (GitHub + GitCode 双remote)

架构 (与 GitHub Actions 互补):
  GitHub Actions (保留)   日级数据: 财务/龙虎榜/新闻/复盘 (每 5-30 分钟, 免费额度)
  云服务器 (新增)         分钟级数据: 全市场快照/分时/资金流 (每 60 秒, 常驻)
  网站 (GitHub Pages)     读 GitHub raw 数据, 分钟级延迟
"""
set -e

REPO_DIR="${1:-$(pwd)}"
cd "$REPO_DIR"

echo "=== 1. 环境检查 ==="
python3 --version || { echo "需要 python3.9+"; exit 1; }
pip3 --version || echo "pip3 缺失, 尝试 python3 -m pip"

echo "=== 2. 安装依赖 ==="
pip3 install --quiet duckdb akshare pandas lxml 2>&1 | tail -3 || \
  echo "pip 安装失败, 继续 (系统可能已有)"

echo "=== 3. 建数据目录 ==="
mkdir -p data/market data/adjusted/bulk data/level2 data/research
mkdir -p /root/workspace 2>/dev/null || true

echo "=== 4. 全量回补 (后台, 约 4-6 小时) ==="
# 复权批量 (5268 只 × qfq+hfq)
if [ ! -f data/adjusted/bulk/_progress.json ]; then
  echo "  启动全市场复权采集 (后台)..."
  nohup python3 scripts/collect_adjusted_bulk.py --all --adj both --threads 3 \
    --resume --out-dir data/adjusted/bulk > /tmp/adj_bulk.log 2>&1 &
  echo "  PID=$! 日志: /tmp/adj_bulk.log"
else
  echo "  复权已采过 (_progress.json 存在), 跳过; 要续采加 --resume"
fi

echo "=== 5. 指数全史 + 周期池 (前台, ~3 分钟) ==="
python3 scripts/collect_adjusted.py --index 2>&1 | tail -3 || echo "  指数采集失败"
python3 scripts/cycle_pool_v2.py 2>&1 | tail -8 || echo "  周期池失败"

echo "=== 6. 安装 systemd 服务 (分钟级采集) ==="
SERVICE=/etc/systemd/system/stocks-realtime.service
if [ -w /etc/systemd/system ]; then
  cat > "$SERVICE" <<EOF
[Unit]
Description=stocks-ai-view minute-level realtime collector
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=STOCKS_AI_VIEW=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/scripts/realtime_minute.py --interval 60 --push-every 5
Restart=always
RestartSec=30
StandardOutput=append:/tmp/stocks-realtime.log
StandardError=append:/tmp/stocks-realtime.log

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now stocks-realtime
  systemctl status stocks-realtime --no-pager | head -8 || true
else
  echo "  无 systemd 写权限, 用 nohup 方式启动:"
  nohup python3 scripts/realtime_minute.py --interval 60 --push-every 5 \
    > /tmp/stocks-realtime.log 2>&1 &
  echo "  PID=$! 日志: /tmp/stocks-realtime.log"
fi

echo "=== 7. 配置双 remote (GitHub 主 + GitCode 镜像) ==="
if ! git remote | grep -q gitcode; then
  git remote add gitcode "https://x-access-token:${GC_TOKEN}@gitcode.com/dufuwuxin/stock-system.git" \
    2>/dev/null || echo "  gitcode remote 已存在或 token 未提供"
fi
git remote -v

echo ""
echo "=== 部署完成 ==="
echo "  实时采集: systemctl status stocks-realtime (或 ps aux | grep realtime_minute)"
echo "  日志:      /tmp/stocks-realtime.log, /tmp/adj_bulk.log"
echo "  数据:      $REPO_DIR/data/realtime_minute.json (分钟级)"
echo ""
echo "后续 (可选):"
echo "  - 接入 GitHub Pages 读 raw.githubusercontent.com/qwerzxcva/stocks-ai-view/main/data/realtime_minute.json"
echo "  - 定时任务: crontab -e 加 '0 18 * * 1-5 python3 scripts/collect_level2.py --batch-file ...'"
echo "  - 监控:      tail -f /tmp/stocks-realtime.log"
