from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient, MockTransport, Request, Response
from pytest_mock import MockerFixture

from api.services.internal import InternalService
from api.services.shop import apply_coin_operation


@pytest.mark.parametrize("status,body", [(404, {}), (200, False), (200, {}), (200, {"coins": 0})])
async def test_old_or_invalid_backend_never_falls_back(mocker: MockerFixture, status: int, body: Any) -> None:
    requests: list[Request] = []

    def handle(request: Request) -> Response:
        requests.append(request)
        return Response(status, json=body)

    mocker.patch.object(
        InternalService,
        "client",
        new_callable=property,
        fget=lambda _: AsyncClient(base_url="http://synthetic/_internal", transport=MockTransport(handle)),
    )
    assert await apply_coin_operation(str(uuid4()), str(uuid4()), 10, "Synthetic", False) is False
    assert len(requests) == 1 and requests[0].method == "PUT"
