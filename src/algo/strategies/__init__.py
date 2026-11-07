from algo.strategies.fvg_flow import FvgFlow, FvgSupertrend

REGISTRY = {cls.name: cls for cls in (FvgFlow, FvgSupertrend)}


def get_strategy(name: str, **params):
    if name not in REGISTRY:
        raise KeyError(f"Unknown strategy {name!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[name](**params)
