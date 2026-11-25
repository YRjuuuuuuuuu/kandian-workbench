"""Public DNS validation including VPN fake-IP compatibility; TLS stays verified."""
import functools
import http.client
import ipaddress
import json
import ssl
import urllib.parse


def tls_context():
    context = ssl.create_default_context()
    # Some portable Windows Pythons have no Mozilla CA bundle by default.
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except ImportError:
        try:
            from pip._vendor import certifi
            context.load_verify_locations(certifi.where())
        except ImportError:
            pass
    return context


@functools.lru_cache(maxsize=128)
def resolve_fake_ip(host):
    # Only used for the 198.18/15 synthetic VPN range, never for private DNS.
    if "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("来源不能指向本机或私有网络")
    connection = http.client.HTTPSConnection("1.1.1.1", timeout=8, context=tls_context())
    try:
        connection.request("GET", "/dns-query?" + urllib.parse.urlencode({"name": host, "type": "A"}), headers={"Accept": "application/dns-json"})
        response = connection.getresponse()
        payload = response.read(64001)
        if response.status != 200 or len(payload) > 64000:
            raise ValueError("公共 DNS 查询不可用")
        data = json.loads(payload)
        addresses = sorted({item["data"] for item in data.get("Answer", []) if item.get("type") == 1})
        if data.get("Status") != 0 or not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise ValueError("公共 DNS 未返回有效公网地址")
        return tuple(addresses)
    finally:
        connection.close()
