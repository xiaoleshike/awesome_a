# awesome_a

## A股成交量数据源对比

已提供四个 provider 版本：

| Provider | 实现文件 | 特点 |
|---|---|---|
| mootdx | `stock_volume_mootdx.py` / `stock_volume_compare.py --provider mootdx` | 通达信行情服务器，功能较全，速度取决于可用服务器 |
| easy-tdx | `stock_volume_easytdx.py` / `stock_volume_compare.py --provider easytdx` | 通达信协议封装较轻，适合快速拉取K线 |
| tdx2db | `stock_volume_tdx2db.py` / `stock_volume_compare.py --provider tdx2db` | 读取 tdx2db 已导出的本地 CSV/SQLite，适合本地化批量数据对比 |
| BaoStock | `stock_volume_baostock.py` / `stock_volume_compare.py --provider baostock` | 独立数据平台，免费易用，但可能有频率/覆盖限制 |

安装可选依赖：

```bash
pip install -r requirements-providers.txt
```

快速试跑（默认每个 provider 只取前 10 只股票）：

```bash
python3 stock_volume_compare.py --provider mootdx --limit 10
python3 stock_volume_compare.py --provider easytdx --limit 10
python3 stock_volume_compare.py --provider baostock --limit 10
```

tdx2db 版本需要先准备本地导出数据：

```bash
python3 stock_volume_compare.py --provider tdx2db --tdx2db-csv daily.csv --limit 10
# 或
python3 stock_volume_compare.py --provider tdx2db --tdx2db-sqlite tdx.db --tdx2db-table daily --limit 10
```

全量对比（滚动窗口模式：`end-date + days`，带并行和重试）：

```bash
python3 stock_volume_compare.py \
  --provider mootdx \
  --start-date 20260608 \
  --end-date 20260612 \
  --days 5 \
  --limit 0 \
  --workers 4 \
  --retries 2 \
  --retry-delay 1 \
  --output-dir provider_outputs_full_mootdx_20260608_20260612 \
  --verbose
```

### 主要参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--provider` | `all` | 数据源：mootdx, easytdx, baostock, tdx2db, all |
| `--start-date` | 无 | 开始日期（YYYYMMDD）；传入后进入显式区间模式 |
| `--end-date` | 当天 | 结束日期（YYYYMMDD） |
| `--days` | 20 | 滚动窗口交易日天数；仅用于未传 `--start-date` 的模式 |
| `--limit` | 10 | 取样股票数（0 为全量） |
| `--workers` | 1 | 并行线程数（>1 启用多线程） |
| `--retries` | 0 | 失败重试次数 |
| `--retry-delay` | 0.0 | 重试延迟（秒） |
| `--delay` | 0.1 | 每次请求完成后的节流延迟（秒） |
| `--resume` | 否 | 是否从缓存恢复（重用已成功的记录） |
| `--output-dir` | `provider_outputs` | 输出目录 |
| `--verbose` | 否 | 显示详细进度日志 |

### 两种时间模式

- 滚动窗口模式：传 `--end-date` 和可选 `--days`，表示“截止到某天，向前取 N 个交易日”。
- 显式区间模式：传 `--start-date` 与可选 `--end-date`，程序按这个区间的交易日数量计算，不再接受 `--days`。

例如：

```bash
# 模式 1：截止到 20260615，向前取 20 个交易日
python3 stock_volume_compare.py \
  --provider baostock \
  --end-date 20260615 \
  --days 20

# 模式 2：显式使用 20260608 ~ 20260612 这个区间
python3 stock_volume_compare.py \
  --provider baostock \
  --start-date 20260608 \
  --end-date 20260612
```

### 高性能运行建议

```bash
# 快速扫描（4 线程 + 2 重试）
python3 stock_volume_compare.py \
  --provider baostock \
  --start-date 20260608 \
  --end-date 20260612 \
  --days 5 \
  --limit 0 \
  --workers 4 \
  --retries 2 \
  --retry-delay 1 \
  --verbose

# 失败恢复（仅重试失败股票）
python3 stock_volume_compare.py \
  --provider baostock \
  --start-date 20260608 \
  --end-date 20260612 \
  --days 5 \
  --limit 0 \
  --workers 2 \
  --retries 3 \
  --output-dir provider_outputs_full_baostock_20260608_20260612 \
  --resume \
  --verbose
```

输出目录默认是 `provider_outputs/`：

- `comparison_summary.csv`：运行效率、股票数、成功数、失败数、成功率、完整5日数据覆盖率、平均返回交易日数、结果行数
- `{provider}_high_volume_stocks.csv`：每个 provider 的最终高频成交量股票结果
- `{provider}_top_per_day.json`：每个交易日成交量前300股票
- `accuracy_overlap.csv`：provider 结果之间的集合重合度，用于初步评估准确性/一致性

### 已知实现说明

- `stock_volume_compare.py` 是当前主入口，负责多数据源拉取、频次统计和结果对比。
- `stock_volume_screener.py` 是较早的单数据源实验脚本，默认参数和主入口可能不同，更适合作为参考实现而不是主流程。