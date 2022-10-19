import base64
import inspect
import pickle  # noqa: S403
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar, cast

from api.redis import redis
from api.settings import settings


T = TypeVar("T")


def redis_cached(
    prefix: str, *key: str, ttl: int = settings.cache_ttl
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:

        pos_cnt = 0
        param_indices: dict[str, int] = {}
        for param in inspect.signature(func).parameters.values():
            if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
                if param.name in key:
                    param_indices[param.name] = pos_cnt
                pos_cnt += 1

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            ident = f"{func.__module__}:{func.__name__}"
            k = f"func_cache:{prefix}:{ident}:" + base64.b64encode(
                pickle.dumps(
                    [args[i] if 0 <= (i := param_indices.get(arg, -1)) < len(args) else kwargs[arg] for arg in key]
                )
            ).decode().rstrip("=")
            if res := await redis.get(k):
                return cast(T, pickle.loads(base64.b64decode(res.encode())))  # noqa: S301

            result = await func(*args, **kwargs)
            await redis.setex(k, ttl, base64.b64encode(pickle.dumps(result)))
            return result

        return wrapper

    return decorator


async def clear_cache(prefix: str) -> None:
    if keys := await redis.keys(f"func_cache:{prefix}:*"):
        await redis.delete(*keys)
