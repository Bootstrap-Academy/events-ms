from typing import Any, Awaitable, Callable

from fastapi import Depends, Request
from fastapi.openapi.models import HTTPBearer
from fastapi.security.base import SecurityBase
from pydantic import ValidationError

from .exceptions.auth import EmailNotVerifiedError, InvalidTokenError, PermissionDeniedError, UserNotFoundError
from .schemas.user import User, UserAccessToken
from .services.auth import exists_user
from .utils.jwt import decode_jwt


def get_token(request: Request) -> str:
    authorization: str = request.headers.get("Authorization", "")
    return authorization.removeprefix("Bearer ")


class HTTPAuth(SecurityBase):
    def __init__(self) -> None:
        self.model = HTTPBearer()
        self.scheme_name = self.__class__.__name__

    async def __call__(self, request: Request) -> Any:
        raise NotImplementedError


class JWTAuth(HTTPAuth):
    def __init__(self, *, audience: list[str] | None = None, force_valid: bool = True):
        super().__init__()
        self.audience: list[str] | None = audience
        self.force_valid: bool = force_valid

    async def __call__(self, request: Request) -> dict[Any, Any] | None:
        if (data := decode_jwt(get_token(request), audience=self.audience)) is None and self.force_valid:
            raise InvalidTokenError
        return data


class InternalAuth(JWTAuth):
    def __init__(self, audience: list[str] | None = None):
        super().__init__(audience=audience, force_valid=True)


jwt_auth = Depends(JWTAuth(force_valid=False))
internal_auth = Depends(InternalAuth(audience=["events"]))


@Depends
async def public_auth(data: dict[Any, Any] = jwt_auth) -> User | None:
    try:
        token: UserAccessToken = UserAccessToken.parse_obj(data)
    except (InvalidTokenError, ValidationError):
        return None

    if await token.is_revoked():
        return None

    return token.to_user()


@Depends
async def user_auth(user: User | None = public_auth) -> User:
    if user is None:
        raise InvalidTokenError
    return user


@Depends
async def admin_auth(user: User = user_auth) -> User:
    if not user.admin:
        raise PermissionDeniedError
    return user


@Depends
async def is_admin(user: User | None = public_auth) -> bool:
    return user is not None and user.admin


async def _require_verified_email(user: User = user_auth) -> None:
    if not user.email_verified and not user.admin:
        raise EmailNotVerifiedError


require_verified_email = Depends(_require_verified_email)


def _get_user_dependency(check_existence: bool = False) -> Callable[[str, User | None], Awaitable[str]]:
    async def default_dependency(user_id: str, user: User | None = public_auth) -> str:
        if user_id.lower() in ["me", "self"] and user:
            user_id = user.id
        if check_existence and not await exists_user(user_id):
            raise UserNotFoundError

        return user_id

    return default_dependency


def _get_user_privileged_dependency(check_existence: bool = False) -> Callable[[str, User], Awaitable[str]]:
    async def self_or_admin_dependency(user_id: str, user: User = user_auth) -> str:
        if user_id.lower() in ["me", "self"]:
            user_id = user.id
        if user.id != user_id and not user.admin:
            raise PermissionDeniedError

        return await _get_user_dependency(check_existence)(user_id, None)

    return self_or_admin_dependency


def get_user(*, check_existence: bool = False, require_self_or_admin: bool = False) -> Any:
    return Depends(
        _get_user_privileged_dependency(check_existence)
        if require_self_or_admin
        else _get_user_dependency(check_existence)
    )
