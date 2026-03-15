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
async def test_get_umap_geolinks(mock_exec, async_client):
    mock_process = MagicMock()
    
    async def mock_communicate():
        return (b'{"umaps": {"here": "test", "north": "test", "south": "test", "east": "test", "west": "test", "northeast": "test", "northwest": "test", "southeast": "test", "southwest": "test"}, "sectors": {"here": "test", "north": "test", "south": "test", "east": "test", "west": "test", "northeast": "test", "northwest": "test", "southeast": "test", "southwest": "test"}, "regions": {"here": "test", "north": "test", "south": "test", "east": "test", "west": "test", "northeast": "test", "northwest": "test", "southeast": "test", "southwest": "test"}}', b'')
        
    mock_process.communicate = mock_communicate
    mock_process.returncode = 0
    mock_exec.return_value = mock_process

    response = await async_client.get("/api/geo/umap?lat=48.8566&lon=2.3522")
    # The endpoint might not exist exactly like this, but we test the function logic if possible
    # Assuming there's an endpoint that calls get_umap_geolinks
    # If not, we can test the function directly
    from routers.geo import get_umap_geolinks
    result = await get_umap_geolinks(48.8566, 2.3522)
    assert result["success"] == True
    assert "umaps" in result
