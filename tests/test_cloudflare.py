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

    def test_list_shows_named_ingress_routes(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [
                {"hostname": "app.kurwalab.fyi", "service": "http://127.0.0.1:8080"},
                {"hostname": "api.example.com", "service": "http://127.0.0.1:9000"},
                {"service": "http_status:404"},
            ]}},
        ])
        self.assertEqual(
            routes.list_routes(),
            "Routes on kurwalab-tunnel:\n"
            "- app.kurwalab.fyi -> http://127.0.0.1:8080\n"
            "- api.example.com -> http://127.0.0.1:9000",
        )

    def test_list_reports_when_no_named_routes_exist(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [{"service": "http_status:404"}]}},
        ])
        self.assertEqual(routes.list_routes(), "No routes configured on kurwalab-tunnel.")

    def test_next_port_follows_the_last_local_route(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [
                {"hostname": "first.kurwalab.fyi", "service": "http://127.0.0.1:9000"},
                {"hostname": "external.example.com", "service": "https://example.net"},
                {"hostname": "last.kurwalab.fyi", "service": "http://127.0.0.1:8083"},
                {"service": "http_status:404"},
            ]}},
        ])
        self.assertEqual(routes.next_port(), 8084)

    def test_next_port_starts_at_8080_for_an_empty_route_list(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [{"service": "http_status:404"}]}},
        ])
        self.assertEqual(routes.next_port(), 8080)

    def test_next_port_rejects_exhausted_port_range(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [
                {"hostname": "last.kurwalab.fyi", "service": "http://127.0.0.1:65535"},
            ]}},
        ])
        with self.assertRaisesRegex(CloudflareError, "no next port"):
            routes.next_port()

    def test_delete_removes_named_ingress_and_matching_dns(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"warp-routing": {"enabled": False}, "ingress": [
                {"hostname": "app.kurwalab.fyi", "service": "http://127.0.0.1:8080"},
                {"hostname": "other.kurwalab.fyi", "service": "http://127.0.0.1:9000"},
                {"service": "http_status:404"},
            ]}},
            [{"id": "dns-id", "type": "CNAME", "content": "tunnel-id.cfargotunnel.com."}],
            {},
            {},
        ])
        self.assertEqual(routes.delete_route("APP"), "Deleted route: https://app.kurwalab.fyi.")
        put = routes.calls[3]
        self.assertEqual(put[0], "PUT")
        self.assertEqual(put[2]["config"]["warp-routing"], {"enabled": False})
        self.assertEqual(put[2]["config"]["ingress"], [
            {"hostname": "other.kurwalab.fyi", "service": "http://127.0.0.1:9000"},
            {"service": "http_status:404"},
        ])
        self.assertEqual(routes.calls[4][:2],
                         ("DELETE", "/zones/" + "b" * 32 + "/dns_records/dns-id"))

    def test_delete_requires_an_existing_route(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [{"service": "http_status:404"}]}},
        ])
        with self.assertRaisesRegex(CloudflareError, "was not found"):
            routes.delete_route("app")
        self.assertEqual(len(routes.calls), 2)

    def test_delete_does_not_remove_unrelated_dns(self):
        routes = FakeRoutes([
            [{"id": "tunnel-id", "name": "kurwalab-tunnel", "tun_type": "cfd_tunnel"}],
            {"config": {"ingress": [
                {"hostname": "app.kurwalab.fyi", "service": "http://127.0.0.1:8080"},
                {"service": "http_status:404"},
            ]}},
            [{"id": "dns-id", "type": "A", "content": "192.0.2.1"}],
        ])
        with self.assertRaisesRegex(CloudflareError, "not managed by this tunnel"):
            routes.delete_route("app")
        self.assertEqual(len(routes.calls), 3)


if __name__ == "__main__":
    unittest.main()
