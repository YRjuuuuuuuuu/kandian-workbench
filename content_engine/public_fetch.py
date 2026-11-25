"""Bounded public HTTPS with public-DNS checks, verified TLS and system routing."""
import gzip
import http.client
import io
import socket
import ssl
import urllib.parse
import urllib.request
import urllib.error
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from contextlib import contextmanager

from .sources import public_addresses
from .network import tls_context


def fetch(url, limit=4_000_000, timeout=9, accept="*/*"):
    native = _native_curl()
    if native:
        return _curl_fetch(native, url, limit, timeout, accept)
    # Retry a broken transport once. HTTP errors and access controls are final.
    for attempt in range(2):
        try:
            return _fetch(url, limit, timeout, accept)
        except (ssl.SSLError, ConnectionError, TimeoutError):
            if attempt:
                raise


def _native_curl():
    path = Path(os.environ.get("WINDIR", "C:/Windows"))/"System32"/"curl.exe"
    return str(path) if os.name == "nt" and path.is_file() else None


@contextmanager
def _scratch():
    root=Path(__file__).resolve().parents[1]/"data"/".http-tmp"
    root.mkdir(parents=True,exist_ok=True)
    folder=root/("fetch-"+uuid.uuid4().hex)
    # Inherit normal project ACLs. tempfile's Windows 0o700 directory can exclude
    # the native child process under a managed executor's restricted token.
    folder.mkdir()
    try:
        yield folder
    finally:
        for name in ("body","headers"):
            try:(folder/name).unlink(missing_ok=True)
            except OSError:pass
        try:folder.rmdir()
        except OSError:pass


def _curl_fetch(executable, url, limit, timeout, accept):
    # Windows' native Schannel transport follows the user's verified system TLS
    # path. Python/OpenSSL can fail during the same local proxy handshake.
    with _scratch() as folder:
        body, header = Path(folder)/"body", Path(folder)/"headers"
        for _ in range(4):
            public_addresses(url)
            command=[executable,"--globoff","--proto","=https","--max-redirs","0","--silent","--show-error","--max-time",str(timeout),"--max-filesize",str(limit),"--header","Accept: "+accept,"--header","Accept-Encoding: identity","--user-agent","KandianResearch/0.8 (bounded public reading)","--dump-header",str(header),"--output",str(body),"--write-out","%{http_code}\n%{content_type}","--url",url]
            proxy=urllib.request.getproxies().get("https") or urllib.request.getproxies().get("all")
            if proxy:command[1:1]=["--proxy",proxy]
            result=subprocess.run(command,capture_output=True,timeout=timeout+3,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            if result.returncode:
                if result.returncode==63:raise ValueError("来源文件超过本次读取上限")
                raise ValueError("素材原生HTTPS读取失败（网络代码 "+str(result.returncode)+"），未关闭TLS或绕过访问限制")
            meta=result.stdout.decode("utf-8",errors="replace").splitlines()
            code=int(meta[0]) if meta and meta[0].isdigit() else 0
            if code in (301,302,303,307,308):
                import email.parser
                parts=[p for p in header.read_text(encoding="iso-8859-1").split("\n\n") if p.strip()]
                headers=email.parser.Parser().parsestr("\n".join(parts[-1].splitlines()[1:]))
                url=urllib.parse.urljoin(url,headers.get("Location", ""))
                continue
            if code!=200:raise ValueError(f"公开来源返回 HTTP {code}；未绕过访问限制")
            content=body.read_bytes()
            if len(content)>limit:raise ValueError("来源文件超过本次读取上限")
            return content,meta[1] if len(meta)>1 else "application/octet-stream",url
    raise ValueError("来源跳转过多")


def _fetch(url, limit, timeout, accept):
    proxy = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("all")
    if proxy:
        return _proxy_fetch(url, limit, timeout, accept, proxy)
    for _ in range(4):
        parsed, host, addresses = public_addresses(url)
        raw = socket.create_connection((addresses[0], 443), timeout=timeout)
        connection = None
        try:
            connection = http.client.HTTPSConnection(host, timeout=timeout)
            connection.sock = tls_context().wrap_socket(raw, server_hostname=host)
            target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request("GET", target, headers={"Host": host, "User-Agent": "Mozilla/5.0 (compatible; KandianResearch/0.8; bounded public reading)", "Accept": accept, "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urllib.parse.urljoin(url, response.getheader("Location", ""))
                continue
            if response.status != 200:
                raise ValueError(f"公开来源返回 HTTP {response.status}；未绕过访问限制")
            length = response.getheader("Content-Length")
            if length and int(length) > limit:
                raise ValueError("来源文件超过本次读取上限")
            content = response.read(limit + 1)
            if len(content) > limit:
                raise ValueError("来源文件超过本次读取上限")
            encoding = response.getheader("Content-Encoding", "identity").lower()
            if encoding == "gzip":
                content = gzip.GzipFile(fileobj=io.BytesIO(content)).read(limit + 1)
                if len(content) > limit:
                    raise ValueError("来源解压后超过上限")
            elif encoding not in ("identity", ""):
                raise ValueError("不支持来源压缩方式")
            return content, response.getheader("Content-Type", "application/octet-stream"), url
        finally:
            raw.close()
            if connection:
                connection.close()
    raise ValueError("来源跳转过多")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _proxy_fetch(url, limit, timeout, accept, proxy):
    # Use the standard proxy implementation (same transport as API requests),
    # validating each redirect before any new network request. No browser state.
    route = urllib.parse.urlsplit(proxy)
    if route.scheme not in ("http", "https") or not route.hostname:
        raise ValueError("当前系统素材代理协议不受支持；未关闭TLS校验")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http":proxy,"https":proxy}), urllib.request.HTTPSHandler(context=tls_context()), NoRedirect())
    for _ in range(4):
        public_addresses(url)
        request = urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; KandianResearch/0.8)","Accept":accept,"Accept-Encoding":"identity"})
        try:
            response = opener.open(request,timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301,302,303,307,308):
                url = urllib.parse.urljoin(url,exc.headers.get("Location", ""));exc.close();continue
            raise ValueError(f"公开来源返回 HTTP {exc.code}；未绕过访问限制") from None
        with response:
            length = response.getheader("Content-Length")
            if length and int(length)>limit: raise ValueError("来源文件超过本次读取上限")
            content=response.read(limit+1)
            if len(content)>limit: raise ValueError("来源文件超过本次读取上限")
            encoding=response.getheader("Content-Encoding","identity").lower()
            if encoding=='gzip':
                content=gzip.GzipFile(fileobj=io.BytesIO(content)).read(limit+1)
                if len(content)>limit: raise ValueError("来源解压后超过上限")
            elif encoding!='identity': raise ValueError("不支持来源压缩方式")
            return content,response.getheader("Content-Type","application/octet-stream"),url
    raise ValueError("来源跳转过多")
