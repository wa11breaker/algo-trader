from algo.strategies.ema_cross_buy import EmaCrossBuy
from algo.strategies.failed_breakout import FailedBreakoutReversal
from algo.strategies.fvg_flow import FvgFlow
from algo.strategies.london_liquidity_ifvg import LondonLiquidityIfvg
from algo.strategies.orb_buy import OrbBuy
from algo.strategies.orb_vwap_trend import OrbVwapTrend
from algo.strategies.overnight_credit import OvernightCredit
from algo.strategies.short_premium import ShortPremium
from algo.strategies.vwap_ema_trend import VwapEmaTrend
from algo.strategies.trend_pullback import TrendPullback
from algo.strategies.squeeze_breakout import SqueezeBreakout
from algo.strategies.pdhl_retest import PdhlRetest
from algo.strategies.vwap_structure import VwapStructure
from algo.strategies.orb_momentum import OrbMomentum
from algo.strategies.vwap_pullback_spread import VwapPullbackSpread
from algo.strategies.vwap_trend import VwapTrend
from algo.strategies.vwap_sd_fade import VwapSdFade
from algo.strategies.vwap_volume_buy import VwapVolumeBuy

REGISTRY = {cls.name: cls for cls in (EmaCrossBuy, OrbBuy, VwapVolumeBuy, FailedBreakoutReversal, VwapPullbackSpread,
                                         OrbVwapTrend, ShortPremium,
                                         OvernightCredit, VwapTrend, LondonLiquidityIfvg, VwapEmaTrend,
                                         TrendPullback, SqueezeBreakout, PdhlRetest, VwapStructure, OrbMomentum, VwapSdFade, FvgFlow)}


def get_strategy(name: str, **params):
    if name not in REGISTRY:
        raise KeyError(f"Unknown strategy {name!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[name](**params)
