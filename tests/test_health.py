from unittest.mock import MagicMock, patch

from starlette.requests import Request

from app.main import app, database_health, health


def test_health_endpoint() -> None:
    assert health() == {"status": "ok"}


def test_database_health_when_connected() -> None:
    mock_connection = MagicMock()
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_connection

    with patch("app.main.engine.connect", return_value=mock_context):
        response = database_health()

    assert response == {"status": "ok", "database": "connected"}
    mock_connection.execute.assert_called_once()


def test_database_health_when_unavailable() -> None:
    from sqlalchemy.exc import OperationalError

    with patch("app.main.engine.connect", side_effect=OperationalError("connect", {}, Exception("offline"))):
        response = database_health()

    assert response.status_code == 503
    assert response.body == b'{"status":"error","database":"unavailable"}'


def test_health_routes_are_registered() -> None:
    route_paths = {route.path for route in app.routes}
    assert {"/", "/health", "/health/db"}.issubset(route_paths)


def test_home_page_renders() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1234),
            "scheme": "http",
            "app": app,
        }
    )
    home_route = next(route for route in app.routes if getattr(route, "path", None) == "/")
    mock_db = MagicMock()
    mock_db.scalars.return_value = []
    response = home_route.endpoint(request, mock_db)

    assert response.status_code == 200
    assert b"YatraNest" in response.body
