#!/usr/bin/env python3
"""Kalshi API auth + minimal REST client.

Credentials come from the environment: KALSHI_KEY_ID and KALSHI_PEM, or the files
key-id.txt and kalshi-prod.pem in KALSHI_CRED_DIR (default ~/.kalshi). Keep key
material outside the repo. Never log or echo it.

KALSHI_REST_BASE and KALSHI_WS_URL override the production hosts (e.g. to use the
demo exchange, which needs its own keys).
"""
import base64
import json
import os
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REST_BASE = os.environ.get("KALSHI_REST_BASE", "https://external-api.kalshi.com")
WS_URL = os.environ.get("KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2")
CRED_DIR = os.path.expanduser(os.environ.get("KALSHI_CRED_DIR", "~/.kalshi"))


def _default(path_env, filename):
    return os.environ.get(path_env) or os.path.join(CRED_DIR, filename)


class KalshiAuth:
    def __init__(self, pem_path=None, key_id=None):
        pem_path = pem_path or _default("KALSHI_PEM", "kalshi-prod.pem")
        if key_id is None:
            key_id = os.environ.get("KALSHI_KEY_ID")
        if key_id is None:
            with open(os.path.join(CRED_DIR, "key-id.txt")) as f:
                key_id = f.read().strip()
        self.key_id = key_id
        with open(pem_path, "rb") as f:
            self._key = serialization.load_pem_private_key(f.read(), password=None)

    def headers(self, method, path):
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path.split('?')[0]}".encode()
        if isinstance(self._key, Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def ws_headers(self):
        return self.headers("GET", "/trade-api/ws/v2")


class KalshiREST:
    """GET helper. Public market-data endpoints need no auth; pass auth for portfolio/account."""

    def __init__(self, auth=None):
        self.auth = auth

    def get(self, path, params=None, authed=False):
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        headers = self.auth.headers("GET", path) if (authed and self.auth) else {}
        req = urllib.request.Request(REST_BASE + path, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def paged(self, path, params=None, list_key=None, limit_pages=50, authed=False):
        params = dict(params or {})
        out = []
        for _ in range(limit_pages):
            d = self.get(path, params, authed=authed)
            out.extend(d.get(list_key, []))
            cur = d.get("cursor")
            if not cur:
                break
            params["cursor"] = cur
        return out
