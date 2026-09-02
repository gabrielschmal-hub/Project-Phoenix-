import pandas as pd, numpy as np
def download(tickers, period="10y", interval="1mo", auto_adjust=True, group_by="column", threads=True, progress=False):
    syms=[tickers] if isinstance(tickers,str) else list(tickers)
    n={"3y":36,"10y":120,"11y":132,"4y":48}.get(period,120)
    idx=pd.date_range(end="2026-08-01",periods=n,freq="MS"); rng=np.random.default_rng(7); cols={}
    for t in syms:
        px=(1.2 if "EUR" in t else 100.0)*np.cumprod(1+rng.normal(0.005,0.04,n))
        if t=="ZERO":  px[10]=0.0            # zero print -> inf return next month
        if t=="GAPPY": px[20]=np.nan         # missing month
        for f in ("Open","High","Low","Close","Volume"):
            cols[(t,f) if group_by=="ticker" else (f,t)]=px if f!="Volume" else np.full(n,1e6)
    df=pd.DataFrame(cols,index=idx); df.columns=pd.MultiIndex.from_tuples(df.columns); return df
