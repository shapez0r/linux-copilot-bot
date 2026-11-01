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
<<<<<<< HEAD
    def test_list_routes_reads_all_ingress_without_writing(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [
                {"hostname": "app.kurwalab.fyi", "service": "http://127.0.0.1:8080"},
                {"hostname": "other.example.org", "path": "/api", "service": "http://localhost:9000"},
                {"path": "/health", "service": "http_status:200"},
                {"service": "http_status:404"},
            ]}},
        ])
        self.assertEqual(routes.list_routes(),
                         "Routes for kurwalab-tunnel:\n"
                         "app.kurwalab.fyi -> http://127.0.0.1:8080\n"
                         "other.example.org (path: /api) -> http://localhost:9000\n"
                         "* (path: /health) -> http_status:200")
        self.assertEqual([call[0] for call in routes.calls], ["GET", "GET"])

    def test_list_routes_empty_and_invalid_ingress(self):
        tunnel = [{"id": "tunnel-id", "name": "kurwalab-tunnel"}]
        routes = FakeRoutes([tunnel, {"config": {"ingress": [{"service": "http_status:404"}]}}])
        self.assertEqual(routes.list_routes(), "No routes configured for kurwalab-tunnel.")
        routes = FakeRoutes([tunnel, {"config": {"ingress": "invalid"}}])
        with self.assertRaisesRegex(CloudflareError, "invalid ingress list"):
            routes.list_routes()
=======
    def test_list_routes_reads_only_hostname_rules(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel"}],
            {"config": {"ingress": [
                {"hostname": "app.kurwalab.fyi", "service": "http://127.0.0.1:8080"},
                {"service": "http_status:404"},
            ]}},
        ])
        self.assertIn("https://app.kurwalab.fyi -> http://127.0.0.1:8080", routes.list_routes())
        self.assertEqual([method for method, _, _ in routes.calls], ["GET", "GET"])

    def test_list_routes_empty(self):
        routes = FakeRoutes([[{"id": "tunnel-id", "name": "kurwalab-tunnel"}],
                             {"config": {"ingress": [{"service": "http_status:404"}]}}])
        self.assertIn("No hostname routes", routes.list_routes())
>>>>>>> c251461 (cloudflare menu)

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

    def test_delete_removes_matching_ingress_and_dns_record(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [
                {"hostname": "app.kurwalab.fyi", "service": "http://127.0.0.1:8080"},
                {"hostname": "other.kurwalab.fyi", "service": "http://127.0.0.1:9000"},
                {"service": "http_status:404"},
            ]}},
            {},
            [{"id": "record"}],
            {},
        ])
        self.assertEqual(routes.delete("app"),
                         "Deleted Cloudflare route https://app.kurwalab.fyi.")
        self.assertEqual(routes.calls[2][2]["config"]["ingress"], [
            {"hostname": "other.kurwalab.fyi", "service": "http://127.0.0.1:9000"},
            {"service": "http_status:404"},
        ])
        self.assertEqual(routes.calls[4],
                         ("DELETE", "/zones/" + "b" * 32 + "/dns_records/record", None))

    def test_delete_missing_route_is_a_no_op(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [{"service": "http_status:404"}]}},
            [],
        ])
        self.assertEqual(routes.delete("app"),
                         "Cloudflare route https://app.kurwalab.fyi was not configured.")
        self.assertEqual([call[0] for call in routes.calls], ["GET", "GET", "GET"])


if __name__ == "__main__":
    unittest.main()
