# A股 ABC 在线交易系统 V2

面向手机浏览器的私有 A 股交易研究/机械执行网站。策略核心仍是 V18：

- **周线只做上一完成周大趋势过滤**
- **A**：日线老压力突破 → 离开 → 回踩 → 二次启动
- **B**：日线强启动 → 第一次极缩量回踩 → 二次启动
- **C**：强 risk-on 环境 → 高位横强 → 放量二次启动（卫星仓）

V2 的目标不是再改策略，而是把研究变成可部署、可复现、可手机执行的系统。

## V2 新增

### 1. NEXT_OPEN 完整动态回测

不再只是固定 D20/D35 诊断。

- T 日信号冻结
- T+1 开盘实际成交
- 同一个 FAIL 不变
- 用真实 T+1 entry 重算结构风险与仓位
- 用真实 entry 重算 MFE
- A/B/C 各自动态卖出状态机重新运行
- 开盘涨停保守跳过
- `entry <= FAIL` 直接跳过
- 支持滑点、佣金、卖出税费
- 自动输出隔夜 gap 分层

### 2. K5/K7 账户级回测

真正逐日 mark-to-market：

- 净值曲线
- CAGR
- 最大回撤
- 回撤峰值 / 谷底 / 恢复日期
- 最长水下时间
- 同时持仓数
- 满仓拒绝
- C最多1只
- A/B > C，满仓新 A/B 到来时 C 可让位
- 同日信号 Monte Carlo 容量压力测试

### 3. 个股结构图

K线直接画：

- MA10 / MA20
- A：压力 P、突破、回踩确认区
- B：强启动区、第一次回踩区
- C：高位横盘 box、小平台高
- 买点、FAIL、动态退出

### 4. 手机每日工作流

首页只显示最新完成交易日正式信号。

第二天开盘后输入实际成交价，网页直接重新计算：

- 真实结构风险
- 最终目标仓位

## 本地启动

### Python

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
make test
make dev
```

打开：

`http://127.0.0.1:8000`

默认开发密码是 `.env` 中的 `APP_PASSWORD`。

### Docker + PostgreSQL

```bash
cp .env.example .env
docker compose up --build
```

## 数据

### 导入原始 Parquet（复现 Golden 推荐）

```bash
python scripts/import_parquet_dir.py /path/to/日线数据
python scripts/scan_today.py
```

### 自动更新

```bash
python scripts/update_data.py
python scripts/scan_today.py
```

或：

```bash
python scripts/run_daily.py
```

支持：

- AKShare
- Tushare

## 页面

- `/`：明日正式候选
- `/screener`：最近 N 个交易日 A/B/C 选股
- `/stock/{code}`：个股结构审计图
- `/execution`：次日实际成交价 → 最终仓位
- `/backtest`：Close vs NEXT_OPEN 完整动态回测
- `/portfolio`：K5/K7 账户净值与回撤
- `/validation`：Golden A78/B61/C59/198

## GitHub / Render

工程已经按 GitHub 仓库根目录组织，包含：

- `.github/workflows/ci.yml`
- `Dockerfile`
- `docker-compose.yml`
- `render.yaml`（短期测试）
- `render.production.yaml`（长期）

详细部署步骤：

[docs/DEPLOY_RENDER.md](docs/DEPLOY_RENDER.md)

## 研究纪律

1. Golden event set 不通过，不继续优化参数。
2. NEXT_OPEN 是新的执行模型，不能引用 Close-entry 的旧胜率/PF。
3. 固定 D10/D20/D35/D60 只允许做诊断，不是正式卖出。
4. C 仍是卫星发动机，不和 A/B 平级占用账户风险预算。
5. 网站不构成收益保证。
