"""Cloudflare Tunnel published-hostname and DNS configuration."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .config import Config

API = "https://api.cloudflare.com/client/v4"
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_HTTP = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})/?$")
FIRST_APP_PORT = 8080


class CloudflareError(RuntimeError):
    """Raised when a tunnel route cannot be managed safely."""


class CloudflareRoutes:
    def __init__(self, cfg: Config):
        self._token = cfg.cloudflare_api_token
        self._account = cfg.cloudflare_account_id
        self._zone = cfg.cloudflare_zone_id
        self._tunnel_name = cfg.cloudflare_tunnel
        self._domain = cfg.cloudflare_domain
        self._tunnel_id = None

    def _request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            API + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            try:
                errors = json.loads(detail).get("errors", [])
                detail = "; ".join(str(item.get("message", item)) for item in errors) or detail
            except (ValueError, AttributeError):
                pass
            raise CloudflareError(f"Cloudflare API returned HTTP {error.code}: {detail[:500]}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise CloudflareError(f"Cloudflare API request failed: {error}") from None
        if not payload.get("success"):
            errors = payload.get("errors") or []
            detail = "; ".join(str(item.get("message", item)) for item in errors) or "unknown error"
            raise CloudflareError(f"Cloudflare API rejected the request: {detail}")
        return payload.get("result")

    def _resolve_tunnel(self) -> str:
        if self._tunnel_id:
            return self._tunnel_id
        query = urllib.parse.urlencode({"name": self._tunnel_name, "is_deleted": "false", "per_page": 1000})
        result = self._request("GET", f"/accounts/{self._account}/tunnels?{query}")
        matches = [item for item in (result or [])
                   if item.get("name") == self._tunnel_name and item.get("tun_type", "cfd_tunnel") == "cfd_tunnel"]
        if len(matches) != 1:
            raise CloudflareError(
                f"expected one active tunnel named {self._tunnel_name!r}, found {len(matches)}")
        self._tunnel_id = matches[0].get("id")
        if not self._tunnel_id:
            raise CloudflareError(f"tunnel {self._tunnel_name!r} has no id")
        return self._tunnel_id

    def _hostname(self, app: str) -> str:
        label = app.lower()
        if not _DNS_LABEL.fullmatch(label):
            raise CloudflareError(
                f"route {app!r} is not a valid DNS label; use only letters, digits and hyphens")
        return f"{label}.{self._domain}"

    def _configuration(self, tunnel_id: str):
        result = self._request("GET", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations")
        config = dict((result or {}).get("config") or {})
        ingress = config.get("ingress")
        if ingress is None:
            ingress = []
        if not isinstance(ingress, list):
            raise CloudflareError("the tunnel configuration has an invalid ingress list")
        return config, ingress

    def _update_ingress(self, tunnel_id: str, hostname: str, service: str):
        config, ingress = self._configuration(tunnel_id)

        route = {"hostname": hostname, "service": service, "originRequest": {}}

        def is_catch_all(rule):
            return not rule.get("hostname") and not rule.get("path")

        updated = []
        route_added = False
        for rule in ingress:
            if (rule.get("hostname") or "").lower() == hostname:
                if not route_added:
                    updated.append(route)
                    route_added = True
            else:
                updated.append(rule)
        if not route_added:
            catch_all = next((index for index, rule in enumerate(updated) if is_catch_all(rule)), len(updated))
            updated.insert(catch_all, route)
        if not any(is_catch_all(rule) for rule in updated):
            updated.append({"service": "http_status:404"})
        config["ingress"] = updated
        self._request("PUT", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations",
                      {"config": config})

    def _upsert_dns(self, tunnel_id: str, hostname: str):
        query = urllib.parse.urlencode({"name": hostname, "per_page": 100})
        records = self._request("GET", f"/zones/{self._zone}/dns_records?{query}") or []
        if len(records) > 1:
            raise CloudflareError(f"multiple DNS records already exist for {hostname}")
        target = f"{tunnel_id}.cfargotunnel.com"
        body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True}
        if not records:
            self._request("POST", f"/zones/{self._zone}/dns_records", body)
            return
        record = records[0]
        if record.get("type") != "CNAME":
            raise CloudflareError(f"the existing DNS record for {hostname} is not a CNAME")
        if record.get("content", "").rstrip(".").lower() == target and record.get("proxied") is True:
            return
        record_id = record.get("id")
        if not record_id:
            raise CloudflareError(f"the existing DNS record for {hostname} has no id")
        self._request("PATCH", f"/zones/{self._zone}/dns_records/{record_id}", body)

    def _deletable_dns_record(self, tunnel_id: str, hostname: str):
        query = urllib.parse.urlencode({"name": hostname, "per_page": 100})
        records = self._request("GET", f"/zones/{self._zone}/dns_records?{query}") or []
        if len(records) > 1:
            raise CloudflareError(f"multiple DNS records already exist for {hostname}")
        if not records:
            return None
        record = records[0]
        target = f"{tunnel_id}.cfargotunnel.com"
        if (record.get("type") != "CNAME" or
                record.get("content", "").rstrip(".").lower() != target):
            raise CloudflareError(f"the DNS record for {hostname} is not managed by this tunnel")
        record_id = record.get("id")
        if not record_id:
            raise CloudflareError(f"the existing DNS record for {hostname} has no id")
        return record_id

    def prepare(self, app: str, port: int) -> str:
        """Create or update an explicitly requested ingress rule and proxied CNAME."""
        hostname = self._hostname(app)
        tunnel_id = self._resolve_tunnel()
        service = f"http://127.0.0.1:{port}"
        self._update_ingress(tunnel_id, hostname, service)
        self._upsert_dns(tunnel_id, hostname)
        return f"Tunnel: https://{hostname} -> {service}."

    def list_routes(self) -> str:
        """List named ingress rules on the configured tunnel."""
        tunnel_id = self._resolve_tunnel()
        _, ingress = self._configuration(tunnel_id)
        routes = [
            f"{rule['hostname']} -> {rule.get('service', 'no service')}"
            for rule in ingress if rule.get("hostname")
        ]
        if not routes:
            return f"No routes configured on {self._tunnel_name}."
        return f"Routes on {self._tunnel_name}:\n" + "\n".join(f"- {route}" for route in routes)

    def next_port(self) -> int:
        """Return the port after the last local HTTP route, or 8080 when the list is empty."""
        tunnel_id = self._resolve_tunnel()
        _, ingress = self._configuration(tunnel_id)
        last_port = None
        for rule in ingress:
            if not rule.get("hostname"):
                continue
            match = _LOCAL_HTTP.fullmatch(str(rule.get("service", "")))
            if match and 1 <= int(match.group(1)) <= 65535:
                last_port = int(match.group(1))
        if last_port is None:
            return FIRST_APP_PORT
        if last_port == 65535:
            raise CloudflareError("the last tunnel route uses port 65535; no next port is available")
        return last_port + 1

    def delete_route(self, app: str) -> str:
        """Delete one explicitly named ingress rule and its tunnel CNAME."""
        hostname = self._hostname(app)
        tunnel_id = self._resolve_tunnel()
        config, ingress = self._configuration(tunnel_id)
        updated = [rule for rule in ingress if (rule.get("hostname") or "").lower() != hostname]
        if len(updated) == len(ingress):
            raise CloudflareError(f"route {hostname} was not found")
        record_id = self._deletable_dns_record(tunnel_id, hostname)
        config["ingress"] = updated
        self._request("PUT", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations",
                      {"config": config})
        if record_id:
            self._request("DELETE", f"/zones/{self._zone}/dns_records/{record_id}")
        return f"Deleted route: https://{hostname}."
