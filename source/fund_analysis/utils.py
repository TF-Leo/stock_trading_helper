import os
import datetime
import pandas as pd
try:
    import akshare as ak
except ImportError:  # P2: 离线时仅用本地缓存回退
    ak = None
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fund_config.json')

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"读取配置文件失败: {e}")
            return {}
    return {}

def save_config(config):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置文件失败: {e}")

FUND_CONFIG = load_config()

def get_data_path(filename):
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    return os.path.join(DATA_DIR, filename)

def load_or_fetch(filename, fetch_func, incremental_func=None):
    path = get_data_path(filename)
    df = None
    last_date = None
    
    if os.path.exists(path):
        try:
            df = pd.read_csv(path, index_col='date', parse_dates=True)
            if not df.empty:
                last_date = df.index[-1].date()
        except Exception as e:
            pass

    today = datetime.date.today()
    need_update = (df is None) or (last_date < today)
    
    if not need_update:
        return df

    # 尝试增量
    if incremental_func and df is not None and last_date:
        try:
            new_data = incremental_func(last_date + datetime.timedelta(days=1))
            if new_data is not None and not new_data.empty:
                df = pd.concat([df, new_data])
                df = df[~df.index.duplicated(keep='last')].sort_index()
                df.to_csv(path)
                return df
        except Exception:
            pass

    # 全量
    try:
        new_data = fetch_func()
        if new_data is not None and not new_data.empty:
            new_data.to_csv(path)
            return new_data
    except Exception:
        pass
    
    return df

def _fetch_fund(code):
    try:
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        if df is None or df.empty: return None
        df = df.rename(columns={'净值日期': 'date', '单位净值': 'close', '日增长率': 'pct_chg'})
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').set_index('date')
        df['close'] = pd.to_numeric(df['close'])
        df['pct_chg'] = df['close'].pct_change()
        return df
    except: return None

def _fetch_index(symbol):
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or df.empty: return None
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').set_index('date')
        df['close'] = pd.to_numeric(df['close'])
        return df
    except: return None

def _fetch_csi_all_share_full():
    return _fetch_csi_all_share_range("20150101", datetime.datetime.now().strftime("%Y%m%d"))

def _fetch_csi_all_share_incremental(start_date):
    end_date_str = datetime.datetime.now().strftime("%Y%m%d")
    start_date_str = start_date.strftime("%Y%m%d")
    return _fetch_csi_all_share_range(start_date_str, end_date_str)

def _fetch_csi_all_share_range(start_date, end_date):
    try:
        df = ak.stock_zh_index_hist_csindex(symbol="000985", start_date=start_date, end_date=end_date)
        if df is None or df.empty: return None
        df = df.rename(columns={'日期': 'date', '收盘': 'close', '成交金额': 'amount_yi'})
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').set_index('date')
        df['close'] = pd.to_numeric(df['close'])
        df['amount_yi'] = pd.to_numeric(df['amount_yi'])
        return df
    except: return None
