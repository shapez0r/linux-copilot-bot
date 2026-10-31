import unittest
from pathlib import Path

from support import make_config
from bot.cloudflare import CloudflareError, CloudflareRoutes


class FakeRoutes(CloudflareRoutes):
    def __init__(self, responses):
        cfg = make_config(
            Path("/tmp/cloudflare-route-test"),
            cloudflare_api_token="token",
            cloudflare_account_id="a" * 32,
            cloudflare_zone_id="b" * 32,
            cloudflare_tunnel="kurwalab-tunnel",
            cloudflare_domain="kurwalab.fyi",
        )
        super().__init__(cfg)
        self.responses = list(responses)
        self.calls = []

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.responses.pop(0)


class CloudflareRouteTests(unittest.TestCase):
    def test_prepare_preserves_other_ingress_and_creates_dns(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"warp-routing": {"enabled": False}, "ingress": [
                {"hostname": "other.kurwalab.fyi", "service": "http://127.0.0.1:9000"},
                {"service": "http_status:404"},
            ]}},
            {},
            [],
            {},
        ])
        message = routes.prepare("EuroFish", 8080)
        self.assertEqual(message, "Tunnel: https://eurofish.kurwalab.fyi -> http://127.0.0.1:8080.")
        put = routes.calls[2]
        self.assertEqual(put[0], "PUT")
        self.assertEqual(put[2]["config"]["warp-routing"], {"enabled": False})
        self.assertEqual(put[2]["config"]["ingress"], [
            {"hostname": "other.kurwalab.fyi", "service": "http://127.0.0.1:9000"},
            {"hostname": "eurofish.kurwalab.fyi", "service": "http://127.0.0.1:8080",
             "originRequest": {}},
            {"service": "http_status:404"},
        ])
        self.assertEqual(routes.calls[4][0], "POST")
        self.assertEqual(routes.calls[4][2]["content"], "tunnel-id.cfargotunnel.com")

    def test_prepare_replaces_its_ingress_and_keeps_matching_dns(self):
        target = "tunnel-id.cfargotunnel.com"
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [
                {"hostname": "app.kurwalab.fyi", "service": "http://127.0.0.1:7000"},
                {"service": "http_status:404"},
            ]}},
            {},
            [{"id": "record", "type": "CNAME", "content": target, "proxied": True}],
        ])
        routes.prepare("app", 8081)
        self.assertEqual(len(routes.calls), 4)
        ingress = routes.calls[2][2]["config"]["ingress"]
        self.assertEqual(ingress[0]["service"], "http://127.0.0.1:8081")

    def test_invalid_app_name_is_rejected_before_api_calls(self):
        routes = FakeRoutes([])
        with self.assertRaisesRegex(CloudflareError, "valid DNS label"):
            routes.prepare("bad_name", 8080)
        self.assertEqual(routes.calls, [])

    def test_tunnel_name_must_resolve_once(self):
        routes = FakeRoutes([[]])
        with self.assertRaisesRegex(CloudflareError, "found 0"):
            routes.prepare("app", 8080)


if __name__ == "__main__":
    unittest.main()
