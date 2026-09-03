from fastapi import APIRouter

from . import users


INTERNAL_ROUTERS: list[APIRouter] = [users.router]
