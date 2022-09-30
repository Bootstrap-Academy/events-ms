"""Test endpoints (to be removed later)"""

from typing import Any

from fastapi import APIRouter


router = APIRouter()


@router.get("/test")
async def test() -> Any:
    """Test endpoint."""

    return {"result": "hello world"}
