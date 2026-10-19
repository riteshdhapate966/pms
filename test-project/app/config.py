from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PMS_", env_file=".env", extra="ignore")

    mode: Literal["mock", "live"] = "mock"
    base_url: str = "http://mock-pms/WebAPI/api/"
    property_code: str = "DEMO"
    source_id: str = "WISH"
    client_id: SecretStr = SecretStr("")
    auth_code: SecretStr = SecretStr("")
    client_id_header: str = "clientID"
    auth_code_header: str = "authCODE"
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    mappings_file: Path = Path(__file__).resolve().parents[1] / "fixtures" / "mappings.json"
    soap_pull_url: str | None = None
    soap_username: SecretStr = SecretStr("")
    soap_password: SecretStr = SecretStr("")
    soap_actions: dict[str, str] = Field(default_factory=dict)
    lifecycle_db: Path = Path(__file__).resolve().parents[1] / "data" / "lifecycle.sqlite3"
    live_contracts_file: Path | None = None
    json_paths: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_connection(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use an HTTP(S) base URL without credentials, query or fragment")
        names = [self.client_id_header.lower(), self.auth_code_header.lower()]
        reserved = {"host", "content-type", "accept", "content-length"}
        if names[0] == names[1] or any(name in reserved for name in names):
            raise ValueError("Authentication headers must be distinct and not reserved")
        if any(
            not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name)
            for name in names
        ):
            raise ValueError("Invalid authentication header name")
        # Missing credentials are reported by readiness/operation guards, so the live UI can start.
        self.base_url = self.base_url.rstrip("/") + "/"
        return self
