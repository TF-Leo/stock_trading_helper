import os
import json

from stock_analysis.data_loader import get_market_index_daily

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_config.json')


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(config):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


def get_market_regime_series(start_date='20200101'):
    df = get_market_index_daily("000985", start_date=start_date, source='sina')
    if df is None or df.empty:
        df = get_market_index_daily("sh000001", start_date=start_date, source='sina')
    if df is None or df.empty:
        return None
    df['MA60'] = df['close'].rolling(60).mean()
    df['MA120'] = df['close'].rolling(120).mean()

    def label_regime(row):
        if row['close'] > row['MA60'] and row['MA60'] > row['MA120']:
            return 'Bull'
        elif row['close'] < row['MA60'] and row['MA60'] < row['MA120']:
            return 'Bear'
        else:
            return 'Shock'

    return df.apply(label_regime, axis=1)
