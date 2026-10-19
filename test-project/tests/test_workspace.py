from html.parser import HTMLParser

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class TabParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.tabs = {}
        self.panels = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
        if values.get("role") == "tab":
            self.tabs[values["data-tab"]] = values["aria-controls"]
        if values.get("role") == "tabpanel":
            self.panels.add(values["id"])


def test_workspace_tabs_have_unique_panels_and_assets(tmp_path):
    settings = Settings(_env_file=None, mode="mock", lifecycle_db=tmp_path / "test.sqlite3")
    with TestClient(create_app(settings)) as client:
        parser = TabParser()
        parser.feed(client.get("/").text)
        assert len(parser.ids) == len(set(parser.ids))
        assert set(parser.tabs) == {
            "properties",
            "rooms",
            "bookings",
            "guests",
            "availability",
            "rates",
            "refunds",
            "activity",
            "settings",
        }
        assert set(parser.tabs.values()) == parser.panels
        for asset in ("tabs.js", "tabs.css", "app.js", "style.css"):
            assert client.get(f"/static/{asset}").status_code == 200


def test_workspace_config_reports_presence_without_exposing_credentials(tmp_path):
    settings = Settings(
        _env_file=None,
        mode="mock",
        property_code="RUF01",
        client_id="private-client-id",
        auth_code="private-auth-code",
        soap_username="private-soap-user",
        soap_password="private-soap-password",
        lifecycle_db=tmp_path / "test.sqlite3",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/workspace/config")
        assert response.status_code == 200
        data = response.json()
        assert data["property_code"] == "RUF01"
        assert data["json_credentials_present"] is True
        assert data["soap_credentials_present"] is True
        assert data["live_writes_supported"] is False
        for secret in (
            "private-client-id",
            "private-auth-code",
            "private-soap-user",
            "private-soap-password",
        ):
            assert secret not in response.text


def test_workspace_missing_credentials_are_visible(tmp_path):
    settings = Settings(
        _env_file=None,
        mode="mock",
        client_id="",
        auth_code="",
        soap_username="",
        soap_password="",
        lifecycle_db=tmp_path / "test.sqlite3",
    )
    with TestClient(create_app(settings)) as client:
        data = client.get("/api/workspace/config").json()
        assert data["json_credentials_present"] is False
        assert data["soap_credentials_present"] is False
