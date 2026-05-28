import yfinance as yf

def last_two(sym):
    d = yf.download(sym, period="10d", progress=False, auto_adjust=False)
    if d is None or d.empty:
        return None
    close = d["Close"]
    if hasattr(close, "columns"):      # 単一銘柄でも列がDataFrameになる場合に対応
        close = close.iloc[:, 0]
    close = close.dropna()
    if len(close) < 2:
        return None
    latest, prev = float(close.iloc[-1]), float(close.iloc[-2])
    return latest, prev, (latest / prev - 1) * 100

for s in ["2516.T", "2042.T", "^N225"]:
    try:
        r = last_two(s)
        print(f"{s} -> 終値{r[0]:.2f} 前日{r[1]:.2f} 変化{r[2]:+.2f}%" if r else f"{s} -> no data")
    except Exception as e:
        print(s, "ERR", e)
