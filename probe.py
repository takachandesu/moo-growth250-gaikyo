import yfinance as yf
for s in ["^MTHR", "2516.T", "2042.T", "^N225"]:
    try:
        d = yf.download(s, period="5d", progress=False, auto_adjust=False)
        v = None if d.empty else round(float(d["Close"].dropna().iloc[-1]), 2)
        print(s, "->", v)
    except Exception as e:
        print(s, "ERR", e)
