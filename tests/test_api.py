import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import importlib.util
import sys
import os

# Import 54321.py dynamically since it starts with a number
spec = importlib.util.spec_from_file_location("main_app", "54321.py")
main_app = importlib.util.module_from_spec(spec)
sys.modules["main_app"] = main_app
spec.loader.exec_module(main_app)

app = main_app.app

@pytest.fixture
def client():
    return TestClient(app)

import pytest_asyncio

from httpx import ASGITransport

@pytest_asyncio.fixture
async def async_client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

@pytest.mark.asyncio
async def test_health_check(async_client):
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json().get("status") in ["ok", "healthy"]
