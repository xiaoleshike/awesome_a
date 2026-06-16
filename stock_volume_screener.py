"""
A股高频交易量股票筛选器

功能：
1. 获取全部A股股票代码，过滤掉ETF和指数代码
2. 串行获取近20个交易日每只股票的交易量
3. 每个交易日按交易量排序，取前300名
4. 统计近20个交易日中进入前300次数 >= 5 次的股票，按频次由高到低输出
"""

import time
from collections import Counter

import akshare as ak
import pandas as pd


# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────
TOP_N = 300          # 每个交易日取交易量前 N 名
TRADING_DAYS = 20    # 统计近 N 个交易日
MIN_FREQ = 5         # 进入前300的最少次数阈值
TEST_LIMIT = 10      # 测试模式只取前 N 只股票（None 表示取全部）
REQUEST_DELAY = 0.3  # 每次请求后的等待秒数，避免被限流


# ──────────────────────────────────────────────
# Step 1：获取全部A股股票代码，过滤ETF和指数
# ──────────────────────────────────────────────
def get_a_share_stocks() -> pd.DataFrame:
    """
    返回 DataFrame，列：['code', 'name']
    过滤规则：
      - 代码以 8 或 4 开头的为北交所股票（保留）
      - 代码以 0、3、6 开头的为沪深A股（保留）
      - 排除名称中含有 ETF、LOF、基金、指数 等关键字的标的
      - 排除代码以 999、000、880 开头的指数代码
    """
    print("Step 1: 获取A股股票列表...")
    df = ak.stock_info_a_code_name()
    df.columns = ["code", "name"]

    total_before = len(df)

    # 过滤指数代码（通常以 000 开头且为6位纯数字指数，或 399 开头深证指数）
    index_prefixes = ("999", "880", "8880")
    df = df[~df["code"].str.startswith(index_prefixes)]

    # 过滤名称中含基金/ETF/LOF/指数相关关键字
    fund_keywords = ["ETF", "LOF", "基金", "指数", "债", "期货"]
    pattern = "|".join(fund_keywords)
    df = df[~df["name"].str.contains(pattern, case=False, na=False)]

    # 只保留合法的A股前缀：0（深圳主板/中小板）、3（创业板）、6（上海主板）、
    #                       4/8（北交所）、9（B股，可选过滤）
    valid_prefixes = ("0", "3", "6", "4", "8")
    df = df[df["code"].str.startswith(valid_prefixes)]

    # 排除B股（以 900 开头的上证B股，以 200 开头的深证B股）
    df = df[~df["code"].str.startswith(("900", "200"))]

    df = df.reset_index(drop=True)
    print(f"  过滤前: {total_before} 只  →  过滤后: {len(df)} 只 A股")
    return df


# ──────────────────────────────────────────────
# Step 2：串行获取近 TRADING_DAYS 个交易日每只股票的交易量
# ──────────────────────────────────────────────
def fetch_volume_for_stock(code: str) -> pd.DataFrame | None:
    """
    获取单只股票近 TRADING_DAYS 个交易日的日行情，返回 ['date', 'volume'] DataFrame。
    失败时返回 None。
    """
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            adjust="",          # 不复权
            start_date="",      # 留空让 akshare 返回最近数据
            end_date="",
        )
        if df is None or df.empty:
            return None

        # akshare 返回的列名（中文）：日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        df = df[["日期", "成交量"]].copy()
        df.columns = ["date", "volume"]
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").tail(TRADING_DAYS).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"  [警告] 获取 {code} 数据失败: {e}")
        return None


def fetch_all_volumes(stocks: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    串行遍历所有股票，返回 {code: DataFrame} 字典。
    """
    limit = TEST_LIMIT  # 修改为 None 可获取全部股票
    codes = stocks["code"].tolist()
    if limit is not None:
        codes = codes[:limit]
        print(f"\nStep 2: 测试模式，仅获取前 {limit} 只股票的交易量数据...")
    else:
        print(f"\nStep 2: 获取全部 {len(codes)} 只股票近 {TRADING_DAYS} 个交易日成交量...")

    volume_data: dict[str, pd.DataFrame] = {}
    total = len(codes)
    for i, code in enumerate(codes, 1):
        name = stocks.loc[stocks["code"] == code, "name"].values[0]
        print(f"  [{i}/{total}] {code} {name}", end="", flush=True)
        df = fetch_volume_for_stock(code)
        if df is not None and not df.empty:
            volume_data[code] = df
            print(f"  ✓ ({len(df)} 天)")
        else:
            print("  ✗ 跳过")
        time.sleep(REQUEST_DELAY)

    print(f"\n  成功获取 {len(volume_data)} / {total} 只股票数据")
    return volume_data


# ──────────────────────────────────────────────
# Step 3：每个交易日按成交量排序，取前 TOP_N 名
# ──────────────────────────────────────────────
def get_top_stocks_per_day(volume_data: dict[str, pd.DataFrame]) -> dict[str, list[str]]:
    """
    以交易日为键，值为当天成交量排名前 TOP_N 的股票代码列表。
    """
    print(f"\nStep 3: 每个交易日按成交量排序，取前 {TOP_N} 名...")

    # 将所有数据合并为宽表：行=交易日，列=股票代码，值=成交量
    frames = []
    for code, df in volume_data.items():
        tmp = df.set_index("date")["volume"].rename(code)
        frames.append(tmp)

    if not frames:
        print("  [错误] 无可用数据")
        return {}

    wide = pd.concat(frames, axis=1)
    wide = wide.sort_index()

    top_per_day: dict[str, list[str]] = {}
    for date, row in wide.iterrows():
        row_clean = row.dropna()
        top_codes = row_clean.nlargest(TOP_N).index.tolist()
        date_str = str(date.date()) if hasattr(date, "date") else str(date)
        top_per_day[date_str] = top_codes

    print(f"  共处理 {len(top_per_day)} 个交易日")
    return top_per_day


# ──────────────────────────────────────────────
# Step 4：统计进入前300次数，输出频次 >= MIN_FREQ 的股票
# ──────────────────────────────────────────────
def count_frequency(
    top_per_day: dict[str, list[str]],
    stocks: pd.DataFrame,
) -> pd.DataFrame:
    """
    统计各股票进入前 TOP_N 的次数，过滤出 >= MIN_FREQ 次的，按频次降序返回。
    """
    print(f"\nStep 4: 统计进入前 {TOP_N} 次数 >= {MIN_FREQ} 的股票...")

    counter: Counter = Counter()
    for codes in top_per_day.values():
        counter.update(codes)

    result = pd.DataFrame(counter.most_common(), columns=["code", "freq"])
    result = result[result["freq"] >= MIN_FREQ].reset_index(drop=True)

    # 关联股票名称
    result = result.merge(stocks[["code", "name"]], on="code", how="left")
    result = result[["code", "name", "freq"]].sort_values("freq", ascending=False).reset_index(drop=True)

    print(f"  共 {len(result)} 只股票进入前 {TOP_N} 达 {MIN_FREQ} 次及以上\n")
    return result


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    # Step 1
    stocks = get_a_share_stocks()
    print(stocks.head(10).to_string(index=False))

    # Step 2
    volume_data = fetch_all_volumes(stocks)
    if not volume_data:
        print("[错误] 未获取到任何成交量数据，程序退出")
        return

    # Step 3
    top_per_day = get_top_stocks_per_day(volume_data)
    if not top_per_day:
        print("[错误] 未能计算每日前300，程序退出")
        return

    # Step 4
    result = count_frequency(top_per_day, stocks)

    if result.empty:
        print(f"没有股票在近 {TRADING_DAYS} 个交易日内进入前 {TOP_N} 达 {MIN_FREQ} 次及以上")
    else:
        print("=" * 50)
        print(f"近 {TRADING_DAYS} 个交易日内，成交量高频前 {TOP_N} 榜单（频次 >= {MIN_FREQ}）：")
        print("=" * 50)
        print(result.to_string(index=False))

        # 保存到 CSV
        output_file = "high_volume_stocks.csv"
        result.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"\n结果已保存至 {output_file}")


if __name__ == "__main__":
    main()
