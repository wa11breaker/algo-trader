"""How well does the close-location delta proxy (flow_proxy) match the recorded tick delta?

For every recorded footprint day: 1-min futures OHLCV + real delta from ticks, proxy =
vol x (2c - h - l) / (h - l). Prints per-day correlation and sign agreement (bars with |delta| > 0).
"""
from pathlib import Path

import numpy as np
import pandas as pd

root = Path(__file__).resolve().parents[2] / "data" / "footprint" / "NIFTY"
rows = []
for f in sorted(root.glob("*.csv")):
    t = pd.read_csv(f, parse_dates=["ts"]).set_index("ts")
    b = t.ltp.resample("1min").ohlc().dropna()
    b["vol"] = t.vol.resample("1min").sum()
    b["real"] = (t.buy - t.sell).resample("1min").sum()
    rng = (b.high - b.low).where(lambda r: r > 0)
    b["proxy"] = (b.vol * (2 * b.close - b.high - b.low) / rng).fillna(0.0)
    m = b[(b.real != 0) & (b.proxy != 0)]
    rows.append({"day": f.stem, "bars": len(b), "corr": round(b.real.corr(b.proxy), 3),
                 "sign_agree": round(float((np.sign(m.real) == np.sign(m.proxy)).mean()), 3),
                 "sign_agree_big": round(float((np.sign(m.real) == np.sign(m.proxy))[
                     m.real.abs() > m.real.abs().median()].mean()), 3)})
print(pd.DataFrame(rows).to_string(index=False))
