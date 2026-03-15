import pytest
import httpx
from unittest.mock import patch, MagicMock
import importlib.util
import sys

# Import 54321.py dynamically
spec = importlib.util.spec_from_file_location("main_app", "54321.py")
main_app = importlib.util.module_from_spec(spec)
sys.modules["main_app"] = main_app
spec.loader.exec_module(main_app)

app = main_app.app

import pytest_asyncio

from httpx import ASGITransport

@pytest_asyncio.fixture
async def async_client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

import asyncio

@pytest.mark.asyncio
@patch('asyncio.create_subprocess_exec')
async def test_check_society_route(mock_exec, async_client):
    # Mock the subprocess
    mock_process = MagicMock()
    
    async def mock_communicate():
        return (b'{"g1pub": "test_pub", "transactions": []}', b'')
        
    mock_process.communicate = mock_communicate
    mock_process.returncode = 0
    mock_exec.return_value = mock_process

    response = await async_client.get("/check_society")
    assert response.status_code == 200
    assert response.json() == {"g1pub": "test_pub", "transactions": []}

@pytest.mark.asyncio
@patch('asyncio.create_subprocess_exec')
async def test_check_revenue_route(mock_exec, async_client):
    mock_process = MagicMock()
    
    async def mock_communicate():
        return (b'{"g1pub": "test_pub", "revenue": 100}', b'')
        
    mock_process.communicate = mock_communicate
    mock_process.returncode = 0
    mock_exec.return_value = mock_process

    response = await async_client.get("/check_revenue")
    assert response.status_code == 200
    assert response.json() == {"g1pub": "test_pub", "revenue": 100}
