class StockDataError(RuntimeError):
    """Base error for stock search, provider, validation, and cache operations."""


class StockInputError(StockDataError):
    """Invalid stock symbol, date range, frequency, or adjustment setting."""


class StockUniverseError(StockDataError):
    """The A-share code/name table could not be loaded or refreshed."""


class StockProviderError(StockDataError):
    """AKShare failed or returned an incompatible response."""


class StockSchemaError(StockProviderError):
    """Provider fields or data values do not satisfy the normalized schema."""


class StockCacheError(StockDataError):
    """A local cache could not be read or written safely."""
