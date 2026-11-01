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

    def _update_ingress(self, tunnel_id: str, hostname: str, service: str):
        result = self._request("GET", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations")
        config = dict((result or {}).get("config") or {})
        ingress = config.get("ingress")
        if ingress is None:
            ingress = []
        if not isinstance(ingress, list):
            raise CloudflareError("the tunnel configuration has an invalid ingress list")

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

<<<<<<< HEAD
    def _delete_ingress(self, tunnel_id: str, hostname: str) -> bool:
        result = self._request("GET", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations")
        config = dict((result or {}).get("config") or {})
        ingress = config.get("ingress")
        if ingress is None:
            ingress = []
        if not isinstance(ingress, list):
            raise CloudflareError("the tunnel configuration has an invalid ingress list")
        updated = [rule for rule in ingress if (rule.get("hostname") or "").lower() != hostname]
        if len(updated) == len(ingress):
            return False
        config["ingress"] = updated
        self._request("PUT", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations",
                      {"config": config})
        return True

    def _delete_dns(self, hostname: str) -> bool:
        query = urllib.parse.urlencode({"name": hostname, "per_page": 100})
        records = self._request("GET", f"/zones/{self._zone}/dns_records?{query}") or []
        if len(records) > 1:
            raise CloudflareError(f"multiple DNS records already exist for {hostname}")
        if not records:
            return False
        record_id = records[0].get("id")
        if not record_id:
            raise CloudflareError(f"the existing DNS record for {hostname} has no id")
        self._request("DELETE", f"/zones/{self._zone}/dns_records/{record_id}")
        return True

    def list_routes(self) -> str:
        """List configured ingress routes without changing tunnel or DNS state."""
        tunnel_id = self._resolve_tunnel()
        result = self._request("GET", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations")
        ingress = (result or {}).get("config", {}).get("ingress", [])
        if not isinstance(ingress, list):
            raise CloudflareError("the tunnel configuration has an invalid ingress list")
        routes = []
        for rule in ingress:
            if not isinstance(rule, dict):
                raise CloudflareError("the tunnel configuration has an invalid ingress rule")
            hostname = rule.get("hostname")
            path = rule.get("path")
            if hostname or path:
                label = str(hostname or "*") + (f" (path: {path})" if path else "")
                routes.append(f"{label} -> {rule.get('service', 'unspecified')}")
        return (f"Routes for {self._tunnel_name}:\n" + "\n".join(routes)
                if routes else f"No routes configured for {self._tunnel_name}.")
=======
    def list_routes(self) -> str:
        """Read the tunnel's configured hostname ingress rules without changing them."""
        tunnel_id = self._resolve_tunnel()
        result = self._request("GET", f"/accounts/{self._account}/cfd_tunnel/{tunnel_id}/configurations")
        ingress = ((result or {}).get("config") or {}).get("ingress") or []
        if not isinstance(ingress, list):
            raise CloudflareError("the tunnel configuration has an invalid ingress list")
        routes = [(rule["hostname"], rule.get("service", "unknown")) for rule in ingress
                  if isinstance(rule, dict) and rule.get("hostname")]
        if not routes:
            return f"No hostname routes configured on {self._tunnel_name}."
        return "Routes on " + self._tunnel_name + ":\n" + "\n".join(
            f"https://{host} -> {service}" for host, service in routes)
>>>>>>> c251461 (cloudflare menu)

    def prepare(self, app: str, port: int) -> str:
        """Create or update an explicitly requested ingress rule and proxied CNAME."""
        hostname = self._hostname(app)
        tunnel_id = self._resolve_tunnel()
        service = f"http://127.0.0.1:{port}"
        self._update_ingress(tunnel_id, hostname, service)
        self._upsert_dns(tunnel_id, hostname)
        return f"Tunnel: https://{hostname} -> {service}."

    def delete(self, app: str) -> str:
        """Delete an explicitly requested ingress rule and its DNS record."""
        hostname = self._hostname(app)
        tunnel_id = self._resolve_tunnel()
        ingress_deleted = self._delete_ingress(tunnel_id, hostname)
        dns_deleted = self._delete_dns(hostname)
        if not ingress_deleted and not dns_deleted:
            return f"Cloudflare route https://{hostname} was not configured."
        return f"Deleted Cloudflare route https://{hostname}."
