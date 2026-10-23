"""Validated security configuration; secrets never appear in validation errors."""
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    origin: str
    redis_url: str = field(repr=False)
    runtime_dsn: str = field(repr=False)
    identity_dsn: str = field(repr=False)
    namespace: str = "bank"
    session_seconds: int = 1800
    allow_loopback_issuer: bool = False
    oidc_ca_file: str | None = field(default=None,repr=False)
    worker_dsn: str = field(default='',repr=False)

    def __post_init__(self):
        if not all((self.issuer, self.client_id, self.client_secret, self.redis_url,
                    self.runtime_dsn, self.identity_dsn, self.worker_dsn)):
            raise ValueError("Required banking configuration is missing")
        issuer, redirect, origin = map(urlsplit, (self.issuer, self.redirect_uri, self.origin))
        local_issuer = (self.allow_loopback_issuer and issuer.scheme == "http"
                        and issuer.hostname in {"localhost", "127.0.0.1"})
        if issuer.scheme != "https" and not local_issuer:
            raise ValueError("OIDC issuer requires HTTPS (explicit loopback test exception only)")
        if (not issuer.hostname or issuer.query or issuer.fragment or issuer.username
                or self.issuer.endswith("/") or redirect.scheme != "https"
                or origin.scheme != "https" or not origin.hostname
                or origin.path or origin.query or origin.fragment or origin.username
                or (redirect.scheme, redirect.netloc) != (origin.scheme, origin.netloc)
                or redirect.path != "/bank-api/auth/callback" or redirect.query or redirect.fragment):
            raise ValueError("Invalid issuer, callback or browser origin configuration")
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", self.namespace) or not 1 <= self.session_seconds <= 86400:
            raise ValueError("Invalid session namespace or lifetime")
        if urlsplit(self.redis_url).scheme not in {"redis", "rediss"}:
            raise ValueError("Invalid auth store configuration")

    @classmethod
    def from_env(cls, *, load_dotenv=True):
        from store.db import load_env
        if load_dotenv:
            load_env()
        return cls(issuer=os.getenv("OIDC_ISSUER", ""), client_id=os.getenv("OIDC_CLIENT_ID", ""),
                   client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
                   redirect_uri=os.getenv("OIDC_REDIRECT_URI", ""), origin=os.getenv("BANK_ORIGIN", ""),
                   redis_url=os.getenv("BANK_REDIS_URL", ""), runtime_dsn=os.getenv("BANK_DATABASE_URL", ""),
                   identity_dsn=os.getenv("BANK_AUTH_DATABASE_URL", ""),
                   namespace=os.getenv("BANK_NAMESPACE", "bank"),
                   session_seconds=int(os.getenv("BANK_SESSION_SECONDS", "1800")),
                   allow_loopback_issuer=os.getenv("OIDC_ALLOW_LOOPBACK_HTTP") == "true",
                   oidc_ca_file=os.getenv("OIDC_CA_FILE") or None,
                   worker_dsn=os.getenv("BANK_WORKER_DATABASE_URL",''))
