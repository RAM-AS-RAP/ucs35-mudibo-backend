"""Placeholder services must answer 501 instead of an unhandled 500."""

import pytest
from response_helpers import without_request_id


@pytest.mark.asyncio
async def test_not_implemented_error_returns_501(api_client, application) -> None:
    @application.get("/api/v1/test-placeholder")
    async def placeholder() -> None:
        raise NotImplementedError

    response = await api_client.get("/api/v1/test-placeholder")
    assert response.status_code == 501
    assert without_request_id(response.json()) == {
        "error": {"code": "not_implemented", "message": "This endpoint is not implemented yet"}
    }
