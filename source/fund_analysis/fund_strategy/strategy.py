import pandas as pd
import numpy as np
try:
    import akshare as ak
except ImportError:  # P2: 离线时仅用本地缓存回退
    ak = None
import os
import json
import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fund_config.json')

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

BENCHMARKS = {
    "000300": "沪深300",
    "000001": "上证指数",
    "399006": "创业板指",
    "000985": "中证全指",
    "000905": "中证500",
}


def _expected_latest_trade_date():
    """计算期望的最新交易日日期（与 stock_analysis.data_loader.meta 同逻辑）。

    规则：
    - 若当前时间 >= 15:00，期望今日收盘数据（回退到最近工作日）
    - 若当前时间 < 15:00，期望昨日数据（回退到最近工作日）
    - 跳过周末（不处理节假日）
    """
    now = datetime.datetime.now()
    expected = now.date()
    if now.hour < 15:
        expected = expected - datetime.timedelta(days=1)
    while expected.weekday() >= 5:  # 5=周六, 6=周日
        expected = expected - datetime.timedelta(days=1)
    return expected


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(config):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


def fetch_fund_data(code):
    path = os.path.join(DATA_DIR, f'fund_{code}.csv')
    if os.path.exists(path):
        try:
            df = pd.read_csv(path, index_col='date', parse_dates=True)
            if not df.empty:
                if df.index[-1].date() >= _expected_latest_trade_date():
                    return df
        except Exception:
            pass
    print(f"正在获取基金数据: {code} ...")
    try:
        if ak is None:
            raise RuntimeError("akshare未安装，离线回退本地缓存")
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        if df is None or df.empty:
            print(f"获取失败: {code}")
            raise RuntimeError("empty")
        df = df.rename(columns={'净值日期': 'date', '单位净值': 'close', '日增长率': 'pct_chg'})
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').set_index('date')
        df['close'] = pd.to_numeric(df['close'])
        df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
        df.to_csv(path)
        return df
    except Exception as e:
        print(f"API报错: {e}")
        try:  # P2: 离线/stale回退
            stale = pd.read_csv(path, index_col='date', parse_dates=True)
            if not stale.empty:
                print(f"[WARN] 回退到本地缓存 ({stale.index[-1].date()})")
                return stale
        except Exception:
            pass
        return None


def fetch_index_data(symbol):
    path = os.path.join(DATA_DIR, f'index_{symbol}.csv')
    if os.path.exists(path):
        try:
            df = pd.read_csv(path, index_col='date', parse_dates=True)
            if not df.empty and df.index[-1].date() >= (datetime.date.today() - datetime.timedelta(days=1)):
                return df
        except Exception:
            pass
    print(f"正在获取指数数据: {symbol} ...")
    try:
        if ak is None:
            raise RuntimeError("akshare未安装，离线回退本地缓存")
        prefix = "sh" if symbol.startswith("0") else "sz"
        df = ak.stock_zh_index_daily(symbol=f"{prefix}{symbol}")
        if df is None or df.empty:
            return None
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').set_index('date')
        df.to_csv(path)
        return df
    except Exception as e:
        print(f"指数API报错 {symbol}: {e}")
        try:  # P2: 离线/stale回退
            stale = pd.read_csv(path, index_col='date', parse_dates=True)
            if not stale.empty:
                return stale
        except Exception:
            pass
        return None


def calculate_indicators(df):
    df = df.copy()
    close = df['close']
    df['MA20'] = close.rolling(window=20).mean()
    df['MA60'] = close.rolling(window=60).mean()
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df


def match_benchmark(fund_df, benchmark_dfs):
    best_bench = None
    best_corr = -1
    fund_ret = fund_df['close'].pct_change().dropna()
    for code, name in BENCHMARKS.items():
        if code not in benchmark_dfs or benchmark_dfs[code] is None:
            continue
        bench_df = benchmark_dfs[code]
        bench_ret = bench_df['close'].pct_change().dropna()
        common_idx = fund_ret.index.intersection(bench_ret.index)
        if len(common_idx) < 100:
            continue
        corr = fund_ret.loc[common_idx].corr(bench_ret.loc[common_idx])
        if corr > best_corr:
            best_corr = corr
            best_bench = code
    return best_bench, best_corr
