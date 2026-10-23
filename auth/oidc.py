"""Configured OIDC code/PKCE flow using Authlib, with pinned issuer and algorithms."""
import secrets
from urllib.parse import urlsplit
import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebToken
from authlib.oidc.core import CodeIDToken
from fastapi import HTTPException


class OIDC:
    def __init__(self, settings):
        self.settings = settings

    def check_endpoint(self, endpoint):
        p = urlsplit(endpoint)
        issuer = urlsplit(self.settings.issuer)
        if p.scheme != "https" and not (self.settings.allow_loopback_issuer and
                p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("Insecure OIDC endpoint")
        if p.netloc != issuer.netloc or p.username or p.fragment:
            raise ValueError("Untrusted OIDC endpoint")

    async def metadata(self):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False,
                                     verify=self.settings.oidc_ca_file or True) as client:
            r = await client.get(self.settings.issuer + "/.well-known/openid-configuration")
            r.raise_for_status()
            meta = r.json()
            if meta["issuer"] != self.settings.issuer:
                raise ValueError("Issuer discovery mismatch")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                self.check_endpoint(meta[key])
            keys = await client.get(meta["jwks_uri"])
            keys.raise_for_status()
            return meta, keys.json()

    def client(self, metadata):
        return AsyncOAuth2Client(self.settings.client_id, self.settings.client_secret,
                                 redirect_uri=self.settings.redirect_uri, scope="openid",
                                 code_challenge_method="S256", timeout=5,
                                 verify=self.settings.oidc_ca_file or True,
                                 token_endpoint_auth_method="client_secret_basic")

    async def authorization_url(self, state, record):
        meta, _ = await self.metadata()
        async with self.client(meta) as client:
            url, _ = client.create_authorization_url(meta["authorization_endpoint"], state=state,
                        nonce=record["nonce"], code_verifier=record["verifier"], prompt="login")
            return url

    def validate_token(self, token, jwks, nonce):
        claims = JsonWebToken(["RS256"]).decode(token["id_token"], jwks,
            claims_cls=CodeIDToken,
            claims_options={"iss": {"essential": True, "value": self.settings.issuer},
                            "aud": {"essential": True, "value": self.settings.client_id},
                            "exp": {"essential": True}, "iat": {"essential": True},
                            "sub": {"essential": True}},
            claims_params={"nonce": nonce, "client_id": self.settings.client_id,
                           "access_token": token.get("access_token")})
        claims.validate(leeway=5)
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError("Invalid subject")
        return claims["sub"]

    async def exchange(self, code, record):
        try:
            meta, keys = await self.metadata()
            async with self.client(meta) as client:
                token = await client.fetch_token(meta["token_endpoint"], code=code,
                                                code_verifier=record["verifier"])
            return self.validate_token(token, keys, record["nonce"])
        except (httpx.TimeoutException, httpx.NetworkError):
            raise HTTPException(503, "Identity provider unavailable") from None
        except Exception:
            raise HTTPException(401, "Invalid identity response") from None
