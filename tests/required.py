"""Assert that a fixture/query produced the value required by the test."""

from inspect import unwrap
from typing import Awaitable, Callable, TypeVar, cast


T = TypeVar("T")


def required(value: T | None) -> T:
    assert value is not None
    return value


def unwrapped(function: Callable[..., Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    """Run the real coroutine inside the test-owned transaction."""
    return cast(Callable[[], Awaitable[None]], unwrap(function))
