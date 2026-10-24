"""Load each strategy only when requested; EMA runners do not need pandas/NumPy."""
__all__ = ['ADXCrossoverStrategy']


def __getattr__(name):
    if name == 'ADXCrossoverStrategy':
        from strategies.adx_crossover import ADXCrossoverStrategy
        globals()[name] = ADXCrossoverStrategy
        return ADXCrossoverStrategy
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
