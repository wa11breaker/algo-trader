"""Write the round-2 sweep grids (YAML lists of param dicts). Run: python research/round2/make_grids.py"""
import itertools
from pathlib import Path

import yaml

EXITS = [{"target_r": 1.5}, {"target_r": 2.0}, {"target_r": 3.0},
         {"target_r": None, "trail_start_r": 1.0, "trail_atr_mult": 2.0}]
SPACE = {
    "trend_pullback": {"impulse_atr": [2.0, 3.0, 4.0], "pb_ema": [9, 20], "adx_min": [0.0, 25.0]},
    "squeeze_breakout": {"bw_pct": [10.0, 20.0, 30.0], "min_squeeze_bars": [4, 8], "stop_at": ["box", "mid"]},
    "pdhl_retest": {"touch_atr": [0.2, 0.5], "max_wait": [6, 12], "confirm_mode": ["break_high", "any"],
                    "break_atr": [0.0, 0.3]},
    "vwap_structure": {"swing_len": [2, 3], "vwap_touch_atr": [None, 1.0], "rejection": ["none", "pattern"]},
    "orb_momentum": {"range_minutes": [15, 30], "body_atr": [0.5, 1.0], "rsi_min": [55.0, 65.0],
                     "stop_at": ["mid", "range"]},
}

if __name__ == "__main__":
    here = Path(__file__).parent
    for name, space in SPACE.items():
        keys = list(space)
        grid = [{**dict(zip(keys, combo)), **ex} for combo in itertools.product(*space.values()) for ex in EXITS]
        (here / f"{name}.yaml").write_text(yaml.safe_dump(grid, sort_keys=False))
        print(f"{name}: {len(grid)} configs")
