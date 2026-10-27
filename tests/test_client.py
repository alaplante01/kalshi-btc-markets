"""Request signing and the REST helper in kalshi_client.py. Keys are generated per test."""
import base64
import io
import os
import tempfile
import time
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

import kalshi_client


class SigningTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pem = os.path.join(tmp.name, "key.pem")

    def auth_for(self, key, key_id="test-key"):
        with open(self.pem, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        return kalshi_client.KalshiAuth(pem_path=self.pem, key_id=key_id)

    def test_rsa_pss_signs_timestamp_method_and_path_without_query(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        h = self.auth_for(key).headers("GET", "/trade-api/v2/portfolio/balance?limit=5")
        self.assertEqual(h["KALSHI-ACCESS-KEY"], "test-key")
        self.assertLess(abs(int(h["KALSHI-ACCESS-TIMESTAMP"]) - time.time() * 1000), 5000)
        signed = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/portfolio/balance".encode()
        key.public_key().verify(  # raises InvalidSignature on any mismatch
            base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), signed,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())

    def test_ed25519_websocket_handshake_headers(self):
        key = ed25519.Ed25519PrivateKey.generate()
        h = self.auth_for(key).ws_headers()
        signed = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/ws/v2".encode()
        key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), signed)

    def test_credentials_from_the_environment(self):
        self.auth_for(ed25519.Ed25519PrivateKey.generate())  # writes the PEM
        with mock.patch.dict(os.environ, {"KALSHI_KEY_ID": "from-env", "KALSHI_PEM": self.pem}):
            auth = kalshi_client.KalshiAuth()
        self.assertEqual(auth.key_id, "from-env")


class RESTTest(unittest.TestCase):
    def test_paged_follows_cursors(self):
        rest = kalshi_client.KalshiREST()
        pages = [{"markets": [1, 2], "cursor": "c1"}, {"markets": [3], "cursor": ""}]
        seen = []

        def fake_get(path, params=None, authed=False):
            seen.append(dict(params))
            return pages[len(seen) - 1]

        with mock.patch.object(rest, "get", side_effect=fake_get):
            out = rest.paged("/trade-api/v2/markets", {"limit": 2}, list_key="markets")
        self.assertEqual(out, [1, 2, 3])
        self.assertEqual(seen, [{"limit": 2}, {"limit": 2, "cursor": "c1"}])

    def test_get_signs_only_authed_requests(self):
        auth = mock.Mock()
        auth.headers.return_value = {"KALSHI-ACCESS-KEY": "k"}
        rest = kalshi_client.KalshiREST(auth)
        requests = []

        def fake_urlopen(req, timeout):
            requests.append(req)
            return io.BytesIO(b'{"ok": true}')

        with mock.patch.object(kalshi_client.urllib.request, "urlopen", fake_urlopen):
            rest.get("/trade-api/v2/markets", {"series_ticker": "KXBTC15M"})
            rest.get("/trade-api/v2/portfolio/balance", authed=True)
        public, private = requests
        self.assertEqual(public.full_url,
                         kalshi_client.REST_BASE + "/trade-api/v2/markets?series_ticker=KXBTC15M")
        self.assertIsNone(public.get_header("Kalshi-access-key"))
        self.assertEqual(private.get_header("Kalshi-access-key"), "k")
        auth.headers.assert_called_once_with("GET", "/trade-api/v2/portfolio/balance")


if __name__ == "__main__":
    unittest.main()
