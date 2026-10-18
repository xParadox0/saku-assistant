"""Who is asking. Identity always comes from a verified Cloudflare Access JWT, never from a form field,
query parameter or the unsigned Cf-Access-Authenticated-User-Email header.

DevAuth exists for local development and the demo. The server refuses to start it on a public interface.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

import jwt


class AuthError(Exception):
    """The request carries no valid identity."""


class AuthUnavailable(Exception):
    """Identity could not be checked right now (signing keys unreachable). Not the user's fault."""


@dataclass(frozen=True)
class Identity:
    email: str


class CloudflareAccessAuth:
    def __init__(self, team_domain: str, audience: str, jwks_fetch: Callable[[], dict] | None = None):
        team_domain = team_domain.rstrip("/")
        if not team_domain.startswith("https://") or not team_domain.endswith(".cloudflareaccess.com"):
            raise ValueError("team domain must look like https://<team>.cloudflareaccess.com")
        if not audience:
            raise ValueError("audience (the Access application AUD tag) is required")
        self.issuer = team_domain
        self.audience = audience
        self._fetch = jwks_fetch or self._http_jwks
        self._keys: dict[str, dict] = {}
        self._fetched_at = 0.0

    def _http_jwks(self) -> dict:
        with urllib.request.urlopen(f"{self.issuer}/cdn-cgi/access/certs", timeout=10) as r:
            return json.loads(r.read())

    def _jwk(self, kid: str) -> dict:
        stale = time.time() - self._fetched_at > 3600
        unknown_and_allowed = kid not in self._keys and time.time() - self._fetched_at > 60
        if stale or unknown_and_allowed:
            try:
                fetched = self._fetch()
            except (OSError, ValueError) as e:  # URLError is an OSError; ValueError covers bad JSON
                if not self._keys:
                    raise AuthUnavailable(f"signing keys unreachable: {type(e).__name__}") from None
                fetched = None  # keep serving with the cached keys
            if fetched is not None:
                self._keys = {k["kid"]: k for k in fetched.get("keys", []) if "kid" in k}
                self._fetched_at = time.time()
        if kid not in self._keys:
            raise AuthError("signing key not recognised")
        return self._keys[kid]

    def identify(self, headers) -> Identity:
        token = headers.get("cf-access-jwt-assertion")
        if not token:
            raise AuthError("missing Cloudflare Access token")
        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
            key = jwt.PyJWK(self._jwk(kid))
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                                options={"require": ["exp", "iat", "aud", "iss"]})
        except (AuthError, AuthUnavailable):
            raise
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {type(e).__name__}") from None
        email = (claims.get("email") or "").strip().lower()
        if not email:
            raise AuthError("token has no email claim")
        return Identity(email)


class DevAuth:
    def __init__(self, email: str):
        self.email = email.strip().lower()

    def identify(self, headers) -> Identity:
        return Identity(self.email)
