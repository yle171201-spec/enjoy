# GitHub → Render → 手机网址部署

## 1. GitHub 仓库

建议仓库名：`a-share-abc-system`。

把本工程根目录直接提交到仓库 `main` 分支。根目录必须能看到：

- `Dockerfile`
- `render.yaml`
- `render.production.yaml`
- `requirements.txt`
- `app/`
- `scripts/`
- `.github/workflows/ci.yml`

GitHub CI 会自动：编译 Python、运行 pytest、构建 Docker 镜像。

## 2. 第一次测试部署

Render Dashboard → New → Blueprint → 选择 GitHub 仓库 → Blueprint Path 使用 `render.yaml`。

需要手工填写 secret：

- `APP_PASSWORD`：你手机登录网站的密码
- `TUSHARE_TOKEN`：如果用 Tushare

测试 Blueprint 只创建 Free Web + Free Postgres，不自动创建全市场 Cron；它只用于验证网站可以从 GitHub 部署、登录和访问。完整历史数据库很可能超过 Free Postgres 1GB，因此不要拿测试 Blueprint 跑正式全市场历史。

**注意：Render Free Postgres 只适合短期测试，会过期，不应作为长期实盘数据库。**

Render 部署成功后会生成类似：

`https://abc-a-share-web-v2.onrender.com`

手机浏览器直接打开即可。

## 3. 正式长期版

正式版建议使用 `render.production.yaml`：

- Web: Starter
- PostgreSQL: Basic-256MB 起
- Cron: Standard（2GB，降低全市场扫描 OOM 风险）

原因：A股多年日线数据不能放在临时文件系统，也不应该依赖 30 天到期的免费数据库。

在 Render 创建 Blueprint 时将 Blueprint Path 改为：

`render.production.yaml`

## 4. 数据源

推荐正式环境：

`DATA_PROVIDER=tushare`

并设置 `TUSHARE_TOKEN`。

项目也支持 `akshare`，但线上全市场高频批量更新时，公共接口稳定性可能不如正式 token 数据源。

## 5. 历史数据首次导入

要复现原研究 Golden A78/B61/C59，优先用原始 Parquet 日线数据：

```bash
DATABASE_URL='你的Render外部数据库URL' \
python scripts/import_parquet_dir.py /path/to/日线数据
```

然后：

```bash
DATABASE_URL='你的Render外部数据库URL' \
python scripts/scan_today.py
```

打开网站 `/validation`，确认：

- A=78
- B=61
- C=59
- Combined=198
- Missing=0
- Extra=0

**在 Golden 未通过前，不把网站提示当作实盘正式信号。**

## 6. 每日自动更新

正式 `render.production.yaml` 的 Cron 使用 Standard（2GB）而不是 Starter。原因是 Golden 一致性的全市场扫描仍会加载多年日线，512MB 级实例存在 OOM 风险。

Render Cron：

`30 10 * * 1-5`

Render Cron 使用 UTC，这对应北京时间工作日 18:30。

执行：

```bash
python scripts/run_daily.py
```

流程：

1. 更新股票列表与最新日线
2. 更新数据库
3. 运行完整 V18 A/B/C 扫描
4. 写入最新信号
5. 更新 Golden 状态

## 7. 手机日常使用

收盘后打开首页：

- 明日A买
- 明日B买
- 明日C买
- 信号收盘
- 结构失效价
- 收盘预估仓位

第二天开盘后点“输入次日成交价”：

- 输入实际开盘/计划成交价
- 网站重新计算真实结构风险
- 给出最终目标仓位

账户满仓时，C是卫星仓；新 A/B 信号优先于 C。

## 8. 自定义域名（可选）

Render Web Service 可以绑定自己的域名并配置 HTTPS。
在网站稳定后再做，不影响策略功能。
