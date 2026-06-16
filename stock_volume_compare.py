"""
对比 mootdx / easy-tdx / tdx2db / BaoStock 获取A股成交量榜单的表现。

输出：
  - 每个 provider 的高频成交量结果 CSV
  - comparison_summary.csv：运行效率、成功率、结果行数等
  - accuracy_overlap.csv：不同 provider 结果集合重合度（Jaccard）
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd


TOP_N = 300
TRADING_DAYS = 20
MIN_FREQ = 5
END_DATE = "20260615"
START_DATE = None
REQUEST_DELAY = 0.1
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ensure_pandas_append_compat() -> None:
    if hasattr(pd.DataFrame, "append"):
        return

    def append(self: pd.DataFrame, other: pd.DataFrame, ignore_index: bool = False, **kwargs: object) -> pd.DataFrame:
        return pd.concat([self, other], ignore_index=ignore_index, **kwargs)

    pd.DataFrame.append = append  # type: ignore[attr-defined]


def normalize_code(code: object) -> str:
    text = str(code).strip()
    if "." in text:
        parts = text.split(".")
        text = parts[-1] if parts[0].lower() in {"sh", "sz", "bj"} else parts[0]
    return text.zfill(6)[-6:]


def safe_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def is_a_share_code(code: object) -> bool:
    code = normalize_code(code)
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "4", "8"))


def tdx_market(code: str) -> int | None:
    code = normalize_code(code)
    if code.startswith(("0", "2", "3")):
        return 0
    if code.startswith(("6", "9")):
        return 1
    if code.startswith(("4", "8")):
        return 2
    return None


def baostock_code(code: str) -> str:
    code = normalize_code(code)
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith(("0", "3")):
        return f"sz.{code}"
    return f"bj.{code}"


def normalize_stock_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["code", "name"])
    df = df.copy()
    df["code"] = df["code"].map(normalize_code)
    if "name" not in df.columns:
        df["name"] = ""
    df = df[df["code"].map(is_a_share_code)]
    df = df[~df["name"].astype(str).str.contains("ETF|LOF|基金|指数|债|期货", case=False, na=False)]
    return df[["code", "name"]].drop_duplicates("code").reset_index(drop=True)


def normalize_history_df(df: pd.DataFrame, end_date: str, days: int, start_date: str | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", "volume"])
    df = df.copy()
    if "date" not in df.columns:
        for col in ("datetime", "trade_date", "time"):
            if col in df.columns:
                df["date"] = df[col]
                break
    if "volume" not in df.columns:
        for col in ("vol", "amount", "成交量"):
            if col in df.columns:
                df["volume"] = df[col]
                break
    if "date" not in df.columns or "volume" not in df.columns:
        return pd.DataFrame(columns=["date", "volume"])
    end = pd.to_datetime(end_date)
    start = pd.to_datetime(start_date) if start_date else None
    df = df[["date", "volume"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["date", "volume"])
    df["date"] = df["date"].dt.normalize()
    df = df[df["date"] <= end]
    if start is not None:
        df = df[df["date"] >= start]
    return df.sort_values("date").tail(days).reset_index(drop=True)


def calculate_frequency(
    stocks: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    top_n: int,
    min_freq: int,
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    frames = []
    for code, df in histories.items():
        if not df.empty:
            frames.append(df.set_index("date")["volume"].rename(code))
    if not frames:
        return pd.DataFrame(columns=["code", "name", "freq"]), {}

    wide = pd.concat(frames, axis=1).sort_index()
    top_per_day: dict[str, list[str]] = {}
    for date, row in wide.iterrows():
        top_per_day[str(date.date())] = row.dropna().nlargest(top_n).index.tolist()

    counter: Counter[str] = Counter()
    for codes in top_per_day.values():
        counter.update(codes)

    result = pd.DataFrame(counter.most_common(), columns=["code", "freq"])
    result = result[result["freq"] >= min_freq].reset_index(drop=True)
    result = result.merge(stocks[["code", "name"]], on="code", how="left")
    result = result[["code", "name", "freq"]].sort_values(["freq", "code"], ascending=[False, True])
    return result.reset_index(drop=True), top_per_day


@dataclass
class ProviderRunResult:
    provider: str
    elapsed_seconds: float
    stock_count: int
    success_count: int
    failed_count: int
    success_rate: float
    trading_days: int
    complete_history_count: int
    complete_history_rate: float
    avg_history_days: float
    result_rows: int
    output_csv: str
    errors: list[str]


class VolumeProvider(ABC):
    name: str

    @abstractmethod
    def list_stocks(self, end_date: str) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def history(self, code: str, end_date: str, days: int, start_date: str | None = None) -> pd.DataFrame:
        raise NotImplementedError

    def close(self) -> None:
        return None


class MootdxProvider(VolumeProvider):
    name = "mootdx"

    def __init__(self) -> None:
        from mootdx.quotes import Quotes

        self.client = Quotes.factory(market="std")

    def list_stocks(self, end_date: str) -> pd.DataFrame:
        frames = []
        for market in (0, 1):
            data = self.client.stocks(market=market)
            frame = pd.DataFrame(data)
            if not frame.empty:
                frame = frame.rename(columns={"code": "code", "name": "name"})
                frames.append(frame)
        if not frames:
            return pd.DataFrame(columns=["code", "name"])
        return normalize_stock_df(pd.concat(frames, ignore_index=True))

    def history(self, code: str, end_date: str, days: int, start_date: str | None = None) -> pd.DataFrame:
        raw = self.client.bars(symbol=normalize_code(code), frequency=9, start=0, offset=max(days * 3, 80))
        df = pd.DataFrame(raw)
        return normalize_history_df(df, end_date, days, start_date)


class EasyTdxProvider(VolumeProvider):
    name = "easytdx"

    def __init__(self) -> None:
        try:
            from easytdx import TdxHq_API
        except ImportError:
            from easy_tdx import TdxClient
            from easy_tdx.models.enums import KlineCategory, Market

            self.api = TdxClient(
                host="119.147.212.81",
                port=7709,
                timeout=5,
                auto_reconnect=False,
                heartbeat_interval=0,
            )
            self.api.connect()
            self._easy_tdx_market = {0: Market.SZ, 1: Market.SH, 2: Market.BJ}
            self._easy_tdx_day = KlineCategory.DAY
            self._legacy_api = False
        else:
            self.api = TdxHq_API()
            if hasattr(self.api, "connect_best_ip"):
                self.api.connect_best_ip()
            else:
                self.api.connect("119.147.212.81", 7709)
            self._legacy_api = True

    def list_stocks(self, end_date: str) -> pd.DataFrame:
        frames = []
        markets = (0, 1) if self._legacy_api else (0, 1, 2)
        for market in markets:
            start = 0
            while True:
                api_market = market if self._legacy_api else self._easy_tdx_market[market]
                batch = self.api.get_security_list(api_market, start)
                frame = pd.DataFrame(batch)
                if frame.empty:
                    break
                frames.append(frame)
                if len(frame) < 1000:
                    break
                start += len(frame)
        if not frames:
            return pd.DataFrame(columns=["code", "name"])
        return normalize_stock_df(pd.concat(frames, ignore_index=True))

    def history(self, code: str, end_date: str, days: int, start_date: str | None = None) -> pd.DataFrame:
        market = tdx_market(code)
        if market is None:
            return pd.DataFrame(columns=["date", "volume"])
        if self._legacy_api:
            raw = self.api.get_security_bars(9, market, normalize_code(code), 0, max(days * 3, 80))
        else:
            raw = self.api.get_security_bars(
                self._easy_tdx_market[market],
                normalize_code(code),
                self._easy_tdx_day,
                0,
                max(days * 3, 80),
            )
        df = pd.DataFrame(raw)
        return normalize_history_df(df, end_date, days, start_date)

    def close(self) -> None:
        disconnect = getattr(self.api, "disconnect", None)
        if callable(disconnect):
            disconnect()


class Tdx2dbProvider(VolumeProvider):
    name = "tdx2db"

    def __init__(
        self,
        csv_path: str | None,
        sqlite_path: str | None,
        table: str,
        code_col: str,
        name_col: str,
        date_col: str,
        volume_col: str,
    ) -> None:
        if not csv_path and not sqlite_path:
            raise ValueError("tdx2db provider requires --tdx2db-csv or --tdx2db-sqlite")
        self.csv_path = Path(csv_path) if csv_path else None
        self.sqlite_path = Path(sqlite_path) if sqlite_path else None
        self.table = safe_identifier(table, "tdx2db table")
        self.code_col = safe_identifier(code_col, "tdx2db code column")
        self.name_col = safe_identifier(name_col, "tdx2db name column")
        self.date_col = safe_identifier(date_col, "tdx2db date column")
        self.volume_col = safe_identifier(volume_col, "tdx2db volume column")
        self._csv_df: pd.DataFrame | None = None

    def _load_csv(self) -> pd.DataFrame:
        if self._csv_df is None:
            assert self.csv_path is not None
            self._csv_df = pd.read_csv(self.csv_path)
        return self._csv_df

    def list_stocks(self, end_date: str) -> pd.DataFrame:
        if self.csv_path:
            df = self._load_csv()
            cols = {self.code_col: "code"}
            if self.name_col in df.columns:
                cols[self.name_col] = "name"
            return normalize_stock_df(df.rename(columns=cols))
        assert self.sqlite_path is not None
        with sqlite3.connect(self.sqlite_path) as conn:
            cols = f"{self.code_col} AS code"
            if self.name_col:
                cols += f", {self.name_col} AS name"
            df = pd.read_sql_query(f"SELECT DISTINCT {cols} FROM {self.table}", conn)
        return normalize_stock_df(df)

    def history(self, code: str, end_date: str, days: int, start_date: str | None = None) -> pd.DataFrame:
        code = normalize_code(code)
        if self.csv_path:
            df = self._load_csv()
            subset = df[df[self.code_col].map(normalize_code) == code].copy()
        else:
            assert self.sqlite_path is not None
            with sqlite3.connect(self.sqlite_path) as conn:
                subset = pd.read_sql_query(
                    f"""
                    SELECT {self.date_col} AS date, {self.volume_col} AS volume
                    FROM {self.table}
                    WHERE {self.code_col} = ?
                    ORDER BY {self.date_col}
                    """,
                    conn,
                    params=(code,),
                )
        subset = subset.rename(columns={self.date_col: "date", self.volume_col: "volume"})
        return normalize_history_df(subset, end_date, days, start_date)


class BaoStockProvider(VolumeProvider):
    name = "baostock"

    def __init__(self) -> None:
        ensure_pandas_append_compat()
        import baostock as bs

        self.bs = bs
        login = self.bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"baostock login failed: {login.error_msg}")

    def list_stocks(self, end_date: str) -> pd.DataFrame:
        rs = self.bs.query_stock_basic()
        df = rs.get_data()
        df = df.rename(columns={"code": "code", "code_name": "name"})
        if "type" in df.columns:
            df = df[df["type"].astype(str) == "1"]
        if "status" in df.columns:
            df = df[df["status"].astype(str) == "1"]
        df["code"] = df["code"].map(normalize_code)
        return normalize_stock_df(df)

    def history(self, code: str, end_date: str, days: int, start_date: str | None = None) -> pd.DataFrame:
        end = datetime.strptime(end_date, "%Y%m%d")
        start = (
            datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d")
            if start_date
            else (end - timedelta(days=max(90, days * 5))).strftime("%Y-%m-%d")
        )
        end_text = end.strftime("%Y-%m-%d")
        rs = self.bs.query_history_k_data_plus(
            baostock_code(code),
            "date,code,volume",
            start_date=start,
            end_date=end_text,
            frequency="d",
            adjustflag="3",
        )
        df = rs.get_data().rename(columns={"volume": "volume"})
        return normalize_history_df(df, end_date, days, start_date)

    def close(self) -> None:
        self.bs.logout()


def build_provider(args: argparse.Namespace, name: str) -> VolumeProvider:
    if name == "mootdx":
        return MootdxProvider()
    if name == "easytdx":
        return EasyTdxProvider()
    if name == "tdx2db":
        return Tdx2dbProvider(
            csv_path=args.tdx2db_csv,
            sqlite_path=args.tdx2db_sqlite,
            table=args.tdx2db_table,
            code_col=args.tdx2db_code_col,
            name_col=args.tdx2db_name_col,
            date_col=args.tdx2db_date_col,
            volume_col=args.tdx2db_volume_col,
        )
    if name == "baostock":
        return BaoStockProvider()
    raise ValueError(f"unknown provider: {name}")


def run_provider(args: argparse.Namespace, provider_name: str) -> tuple[ProviderRunResult, pd.DataFrame]:
    start_time = time.perf_counter()
    provider = build_provider(args, provider_name)
    errors: list[str] = []
    histories: dict[str, pd.DataFrame] = {}
    status_rows: list[dict[str, object]] = []
    stocks = pd.DataFrame(columns=["code", "name"])
    output_csv = ""
    try:
        output_path = Path(args.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        history_cache = output_path / f"{provider_name}_histories.csv"
        status_csv = output_path / f"{provider_name}_history_status.csv"

        stocks = provider.list_stocks(args.end_date)
        if args.codes_file:
            codes = pd.read_csv(args.codes_file, dtype=str, header=None).iloc[:, 0].map(normalize_code)
            stocks = stocks[stocks["code"].isin(set(codes))].copy()
        if args.limit:
            stocks = stocks.head(args.limit).copy()
        if args.resume and history_cache.exists():
            cached = pd.read_csv(history_cache, dtype={"code": str})
            for code, group in cached.groupby("code"):
                df = normalize_history_df(group[["date", "volume"]], args.end_date, args.days, args.start_date)
                if not df.empty:
                    histories[normalize_code(code)] = df
        rows = list(stocks.itertuples(index=False))
        total = len(rows)

        def fetch_history(row: object) -> tuple[str, pd.DataFrame | None, str | None]:
            code = row.code
            last_error = "empty history"
            for attempt in range(args.retries + 1):
                try:
                    df = provider.history(code, args.end_date, args.days, args.start_date)
                    if len(df) >= min(args.days, 1):
                        return code, df, None
                    last_error = "empty history"
                except Exception as exc:  # noqa: BLE001 - keep batch running for success-rate comparison
                    last_error = str(exc)
                if attempt < args.retries and args.retry_delay:
                    time.sleep(args.retry_delay)
            return code, None, last_error

        if args.workers <= 1:
            for i, row in enumerate(rows, start=1):
                if row.code in histories:
                    status_rows.append({"code": row.code, "name": row.name, "success": True, "history_days": len(histories[row.code]), "error": "cached"})
                    continue
                code, df, error = fetch_history(row)
                if df is not None:
                    histories[code] = df
                    status_rows.append({"code": code, "name": row.name, "success": True, "history_days": len(df), "error": ""})
                elif error:
                    errors.append(f"{code}: {error}")
                    status_rows.append({"code": code, "name": row.name, "success": False, "history_days": 0, "error": error})
                if args.delay:
                    time.sleep(args.delay)
                if args.verbose and (i % 100 == 0 or i == total):
                    print(f"{provider_name}: {i}/{total}, success={len(histories)}, failed={len(errors)}")
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {}
                row_by_code = {}
                cached_count = 0
                for row in rows:
                    if row.code in histories:
                        status_rows.append({"code": row.code, "name": row.name, "success": True, "history_days": len(histories[row.code]), "error": "cached"})
                        cached_count += 1
                        continue
                    future = executor.submit(fetch_history, row)
                    futures[future] = row.code
                    row_by_code[row.code] = row
                for i, future in enumerate(as_completed(futures), start=1):
                    code, df, error = future.result()
                    row = row_by_code[code]
                    if df is not None:
                        histories[code] = df
                        status_rows.append({"code": code, "name": row.name, "success": True, "history_days": len(df), "error": ""})
                    elif error:
                        errors.append(f"{code}: {error}")
                        status_rows.append({"code": code, "name": row.name, "success": False, "history_days": 0, "error": error})
                    if args.delay:
                        time.sleep(args.delay)
                    done = cached_count + i
                    if args.verbose and (done % 100 == 0 or done == total):
                        print(f"{provider_name}: {done}/{total}, success={len(histories)}, failed={len(errors)}")

        result, top_per_day = calculate_frequency(stocks, histories, args.top_n, args.min_freq)
        history_rows = []
        for code, df in histories.items():
            for row in df.itertuples(index=False):
                history_rows.append({"code": code, "date": row.date, "volume": row.volume})
        pd.DataFrame(history_rows).to_csv(history_cache, index=False, encoding="utf-8-sig")
        pd.DataFrame(status_rows).to_csv(status_csv, index=False, encoding="utf-8-sig")
        output_csv = str(output_path / f"{provider_name}_high_volume_stocks.csv")
        result.to_csv(output_csv, index=False, encoding="utf-8-sig")
        (output_path / f"{provider_name}_top_per_day.json").write_text(
            json.dumps(top_per_day, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        provider.close()

    elapsed = time.perf_counter() - start_time
    success = len(histories)
    failed = len(stocks) - success
    history_lengths = [len(df) for df in histories.values()]
    complete = sum(1 for length in history_lengths if length >= args.days)
    summary = ProviderRunResult(
        provider=provider_name,
        elapsed_seconds=round(elapsed, 3),
        stock_count=len(stocks),
        success_count=success,
        failed_count=failed,
        success_rate=round(success / len(stocks), 4) if len(stocks) else 0.0,
        trading_days=args.days,
        complete_history_count=complete,
        complete_history_rate=round(complete / success, 4) if success else 0.0,
        avg_history_days=round(sum(history_lengths) / success, 2) if success else 0.0,
        result_rows=len(pd.read_csv(output_csv)) if output_csv else 0,
        output_csv=output_csv,
        errors=errors[:20],
    )
    return summary, pd.read_csv(output_csv) if output_csv else pd.DataFrame()


def compare_results(results: dict[str, pd.DataFrame], output_dir: str) -> None:
    rows = []
    names = list(results)
    for left in names:
        for right in names:
            if left >= right:
                continue
            a = set(results[left].get("code", pd.Series(dtype=str)).astype(str))
            b = set(results[right].get("code", pd.Series(dtype=str)).astype(str))
            union = a | b
            rows.append(
                {
                    "left": left,
                    "right": right,
                    "left_count": len(a),
                    "right_count": len(b),
                    "overlap_count": len(a & b),
                    "jaccard": round(len(a & b) / len(union), 4) if union else 0.0,
                }
            )
    pd.DataFrame(rows).to_csv(Path(output_dir) / "accuracy_overlap.csv", index=False, encoding="utf-8-sig")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare A-share volume providers.")
    parser.add_argument("--provider", choices=["mootdx", "easytdx", "tdx2db", "baostock", "all"], default="all")
    parser.add_argument("--start-date", default=START_DATE, help="YYYYMMDD inclusive start date, e.g. 20260608")
    parser.add_argument("--end-date", default=END_DATE, help="YYYYMMDD, e.g. 20260615")
    parser.add_argument("--days", type=int, default=TRADING_DAYS)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--min-freq", type=int, default=MIN_FREQ)
    parser.add_argument("--limit", type=int, default=10, help="first N stocks for trial; 0 means all")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY)
    parser.add_argument("--workers", type=int, default=1, help="parallel history fetch workers; 1 means serial")
    parser.add_argument("--retries", type=int, default=0, help="retry failed or empty history requests per stock")
    parser.add_argument("--retry-delay", type=float, default=0.0, help="seconds to wait between retries")
    parser.add_argument("--output-dir", default="provider_outputs")
    parser.add_argument("--codes-file", default=None, help="optional one-code-per-line CSV/text file limiting stocks to retry")
    parser.add_argument("--resume", action="store_true", help="reuse existing provider histories in output-dir and retry missing stocks")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--tdx2db-csv", default=None, help="tdx2db exported CSV with code/date/volume columns")
    parser.add_argument("--tdx2db-sqlite", default=None, help="tdx2db SQLite database path")
    parser.add_argument("--tdx2db-table", default="daily")
    parser.add_argument("--tdx2db-code-col", default="code")
    parser.add_argument("--tdx2db-name-col", default="name")
    parser.add_argument("--tdx2db-date-col", default="date")
    parser.add_argument("--tdx2db-volume-col", default="volume")
    args = parser.parse_args(argv)
    if args.start_date and args.start_date > args.end_date:
        parser.error("--start-date must be earlier than or equal to --end-date")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.retries < 0:
        parser.error("--retries must be non-negative")
    if args.limit == 0:
        args.limit = None
    if args.start_date:
        args.days = max(args.days, len(pd.bdate_range(args.start_date, args.end_date)))
    return args


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(argv)
    providers = ["mootdx", "easytdx", "tdx2db", "baostock"] if args.provider == "all" else [args.provider]
    summaries = []
    result_dfs = {}

    for provider_name in providers:
        print(f"\n=== Running provider: {provider_name} ===")
        try:
            summary, result = run_provider(args, provider_name)
            summaries.append(summary.__dict__)
            result_dfs[provider_name] = result
            print(f"{provider_name}: success_rate={summary.success_rate}, elapsed={summary.elapsed_seconds}s")
        except Exception as exc:  # noqa: BLE001 - one provider failure should not block comparison
            summaries.append(
                {
                    "provider": provider_name,
                    "elapsed_seconds": 0,
                    "stock_count": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "success_rate": 0,
                    "trading_days": args.days,
                    "complete_history_count": 0,
                    "complete_history_rate": 0,
                    "avg_history_days": 0,
                    "result_rows": 0,
                    "output_csv": "",
                    "errors": [str(exc)],
                }
            )
            print(f"{provider_name}: failed: {exc}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(summaries).to_csv(output_dir / "comparison_summary.csv", index=False, encoding="utf-8-sig")
    compare_results(result_dfs, args.output_dir)
    print(f"\nDone. Summary: {output_dir / 'comparison_summary.csv'}")


if __name__ == "__main__":
    main()
