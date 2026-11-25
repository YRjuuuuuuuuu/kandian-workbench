"""Bounded HTTPS RSS/Atom discovery; never imports feed text as verified facts."""
import datetime as dt
import email.utils
import html
import http.client
import ipaddress
import re
import socket
import ssl
import urllib.parse
import xml.etree.ElementTree as ET


def public_addresses(url, allowed_host=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("来源仅支持无账号信息的标准 HTTPS 地址")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("来源不能指向本机或私有网络")
    if allowed_host and host != allowed_host:
        raise ValueError("不允许跳转到其他域名")
    addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    fake = ipaddress.ip_network("198.18.0.0/15")
    if literal is None and addresses and all(ipaddress.ip_address(addr) in fake for addr in addresses):
        from .network import resolve_fake_ip
        addresses = set(resolve_fake_ip(host))
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise ValueError("来源不能指向本机或私有网络")
    return parsed, host, sorted(addresses)


def fetch_feed(url, etag=None, modified=None):
    initial_host = urllib.parse.urlsplit(url).hostname
    initial_host = initial_host.encode("idna").decode("ascii").lower() if initial_host else ""
    for _ in range(4):
        parsed, host, addresses = public_addresses(url, initial_host)
        raw = socket.create_connection((addresses[0], 443), timeout=12)
        try:
            connection = http.client.HTTPSConnection(host, timeout=12)
            from .network import tls_context
            connection.sock = tls_context().wrap_socket(raw, server_hostname=host)
            headers = {"Host": host, "User-Agent": "LocalContentWorkbench/0.1 (+user-authorized RSS discovery)", "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml", "Accept-Encoding": "identity"}
            if etag:
                headers["If-None-Match"] = etag
            if modified:
                headers["If-Modified-Since"] = modified
            connection.request("GET", urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, "")), headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urllib.parse.urljoin(url, response.getheader("Location", ""))
                continue
            if response.status == 304:
                return None, etag, modified
            if response.status != 200:
                raise ValueError(f"来源返回 HTTP {response.status}，已停止本次读取")
            if response.getheader("Content-Encoding", "identity") not in ("identity", ""):
                raise ValueError("来源返回不支持的压缩格式")
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ValueError("RSS 超出 2MB 上限")
            return body, response.getheader("ETag"), response.getheader("Last-Modified")
        finally:
            raw.close()
            if 'connection' in locals():
                connection.close()
    raise ValueError("来源跳转过多")


def clean(text):
    return html.unescape(re.sub(r"<[^>]*>", " ", text or "")).strip()[:6000]


def parse_feed(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("不支持包含 DTD 或实体声明的 XML")
    root = ET.fromstring(body)
    rows = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        values, link = {}, ""
        for child in item:
            tag = child.tag.split("}")[-1]
            values.setdefault(tag, "".join(child.itertext()))
            if tag == "link" and (not child.attrib.get("rel") or child.attrib.get("rel") == "alternate"):
                link = child.attrib.get("href", "") or child.text or link
        link = link.strip() or values.get("link", "").strip()
        parsed = urllib.parse.urlsplit(link)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            continue
        timestamp = values.get("pubDate") or values.get("published") or values.get("updated")
        when = None
        if timestamp:
            try:
                when = email.utils.parsedate_to_datetime(timestamp)
            except (ValueError, TypeError):
                try:
                    when = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    pass
        if when and when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        rows.append({"title": clean(values.get("title"))[:180], "summary": clean(values.get("description") or values.get("summary") or ""), "url": link, "event_at": when.astimezone(dt.timezone.utc).isoformat() if when else None})
        if len(rows) >= 50:
            break
    return rows
