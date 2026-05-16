"""Rate limiter for hh.ru requests (1 req/sec recommended)."""

from aiolimiter import AsyncLimiter

# 1 request per second for hh.ru
hh_limiter = AsyncLimiter(1, 1.0)
