from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socketserver
import ssl
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


HOSTS = ("enterprise-proxy.test", "redirect-proxy.test")
PINNED_IP = "203.0.113.7"


def _write_test_certificates(directory: Path) -> tuple[Path, Path, Path]:
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PR 7416 Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTS[0])])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in HOSTS]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = directory / "ca.pem"
    cert_path = directory / "leaf.pem"
    key_path = directory / "leaf-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


def _read_headers(sock) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 65536:
            raise RuntimeError("oversized request headers")
    return bytes(data)


def _split_connect_target(target: str) -> tuple[str, int]:
    if target.startswith("["):
        host, _, port = target[1:].partition("]:")
    else:
        host, _, port = target.rpartition(":")
    return host, int(port)


class _EnterpriseProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        connect_headers = _read_headers(self.request).decode("iso-8859-1")
        if not connect_headers:
            return
        first_line = connect_headers.splitlines()[0]
        self.server.connect_requests.append(connect_headers)
        method, target, _version = first_line.split()
        if method != "CONNECT":
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return

        target_host, _target_port = _split_connect_target(target)
        try:
            ipaddress.ip_address(target_host)
        except ValueError:
            pass
        else:
            self.request.sendall(
                b"HTTP/1.1 403 Hostname Required\r\nContent-Length: 0\r\n\r\n"
            )
            return

        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        tls_sock = self.server.tls_context.wrap_socket(self.request, server_side=True)
        try:
            request_headers = _read_headers(tls_sock).decode("iso-8859-1")
            self.server.origin_requests.append((target_host, request_headers))
            response = self.server.responses.get(target_host, ("200 OK", (), b"ok"))
            status, headers, body = response
            header_lines = [
                f"HTTP/1.1 {status}",
                *headers,
                f"Content-Length: {len(body)}",
                "Content-Type: text/plain",
                "Connection: close",
                "",
                "",
            ]
            tls_sock.sendall("\r\n".join(header_lines).encode("ascii") + body)
        finally:
            tls_sock.close()


class _ThreadingProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class EnterpriseProxy:
    def __init__(self, cert_path: Path, key_path: Path, responses=None):
        self.server = _ThreadingProxy(("127.0.0.1", 0), _EnterpriseProxyHandler)
        self.server.connect_requests = []
        self.server.origin_requests = []
        self.server.responses = responses or {}
        self.server.sni_names = []
        self.server.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server.tls_context.load_cert_chain(cert_path, key_path)
        self.server.tls_context.set_servername_callback(
            lambda _sock, name, _ctx: self.server.sni_names.append(name)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _EnterpriseHTTPProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request_headers = _read_headers(self.request).decode("iso-8859-1")
        if not request_headers:
            return
        self.server.requests.append(request_headers)
        _method, target, _version = request_headers.splitlines()[0].split()
        parsed = urllib.parse.urlsplit(target)
        try:
            ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            body = b"http-proxy-ok"
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
        else:
            response = (
                b"HTTP/1.1 403 Hostname Required\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
        self.request.sendall(response)


class EnterpriseHTTPProxy:
    def __init__(self):
        self.server = _ThreadingProxy(("127.0.0.1", 0), _EnterpriseHTTPProxyHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _configure_proxy(url: str, *, target_scheme: str = "https"):
    names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    old = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    proxy_name = f"{target_scheme.upper()}_PROXY"
    os.environ[proxy_name] = url
    os.environ[proxy_name.lower()] = url
    return old


def _restore_env(old):
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _request_lines(proxy: EnterpriseProxy):
    return [headers.splitlines()[0] for headers in proxy.server.connect_requests]


def _connect_targets(proxy: EnterpriseProxy):
    return [line.split()[1] for line in _request_lines(proxy)]


def _host_headers(proxy: EnterpriseProxy):
    result = []
    for _host, request in proxy.server.origin_requests:
        result.append(next(line for line in request.splitlines() if line.lower().startswith("host:")))
    return result


def main() -> int:
    repo = Path(os.environ.get("PR7416_REPO_ROOT", Path(__file__).resolve().parents[2]))
    expect_opt_out = os.environ.get("PR7416_EXPECT_OPT_OUT", "1") == "1"
    backend = repo / "studio" / "backend"
    sys.path.insert(0, str(backend))
    import core.inference.tools as tools_mod

    resolver_calls = []

    def resolver(host, port):
        resolver_calls.append((host, port))
        if host in ("127.0.0.1", "::1", "localhost"):
            return False, "Blocked: refusing to fetch non-public address 127.0.0.1.", ""
        return True, "", PINNED_IP

    tools_mod._validate_and_resolve_host = resolver

    with tempfile.TemporaryDirectory(prefix="pr7416-certs-") as cert_dir:
        ca_path, cert_path, key_path = _write_test_certificates(Path(cert_dir))
        tools_mod._tls_ctx = ssl.create_default_context(cafile=str(ca_path))

        with EnterpriseProxy(cert_path, key_path) as proxy:
            old_env = _configure_proxy(proxy.url)
            try:
                os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "0"
                err, body, _content_type = tools_mod._fetch_url_raw(
                    "https://enterprise-proxy.test:8443/page?q=1"
                )
            finally:
                _restore_env(old_env)
            assert err and "403" in err, (err, body)
            assert _connect_targets(proxy) == [f"{PINNED_IP}:8443"], (
                _request_lines(proxy),
                err,
            )
            assert proxy.server.origin_requests == []

        if not expect_opt_out:
            resolver_calls.clear()
            with EnterpriseProxy(cert_path, key_path) as proxy:
                old_env = _configure_proxy(proxy.url)
                try:
                    os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "1"
                    err, body, _content_type = tools_mod._fetch_url_raw(
                        "https://enterprise-proxy.test:8443/page?q=1"
                    )
                finally:
                    _restore_env(old_env)
                assert err and "403" in err, (err, body)
                assert _connect_targets(proxy) == [f"{PINNED_IP}:8443"]
            print(f"PASS before-state platform={sys.platform} python={sys.version.split()[0]}")
            print("PASS pre-PR code sends pinned-IP CONNECT even when opt-out env is requested")
            return 0

        resolver_calls.clear()
        with EnterpriseProxy(cert_path, key_path) as proxy:
            old_env = _configure_proxy(proxy.url)
            try:
                os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "1"
                err, body, content_type = tools_mod._fetch_url_raw(
                    "https://enterprise-proxy.test:8443/page?q=1"
                )
            finally:
                _restore_env(old_env)
            assert err is None, err
            assert body == "ok"
            assert content_type == "text/plain"
            assert resolver_calls == [("enterprise-proxy.test", 8443)]
            assert _connect_targets(proxy) == ["enterprise-proxy.test:8443"]
            assert proxy.server.sni_names == ["enterprise-proxy.test"]
            assert _host_headers(proxy) == ["Host: enterprise-proxy.test:8443"]
            assert proxy.server.origin_requests[0][1].splitlines()[0] == (
                "GET /page?q=1 HTTP/1.1"
            )

        resolver_calls.clear()
        responses = {
            "enterprise-proxy.test": (
                "302 Found",
                ("Location: https://redirect-proxy.test:9443/final",),
                b"",
            ),
            "redirect-proxy.test": ("200 OK", (), b"redirect-ok"),
        }
        with EnterpriseProxy(cert_path, key_path, responses) as proxy:
            old_env = _configure_proxy(proxy.url)
            try:
                os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "1"
                err, body, _content_type = tools_mod._fetch_url_raw(
                    "https://enterprise-proxy.test:8443/start"
                )
            finally:
                _restore_env(old_env)
            assert err is None, err
            assert body == "redirect-ok"
            assert resolver_calls == [
                ("enterprise-proxy.test", 8443),
                ("redirect-proxy.test", 9443),
            ]
            assert _connect_targets(proxy) == [
                "enterprise-proxy.test:8443",
                "redirect-proxy.test:9443",
            ]
            assert proxy.server.sni_names == [
                "enterprise-proxy.test",
                "redirect-proxy.test",
            ]
            assert _host_headers(proxy) == [
                "Host: enterprise-proxy.test:8443",
                "Host: redirect-proxy.test:9443",
            ]

        resolver_calls.clear()
        responses = {
            "enterprise-proxy.test": (
                "302 Found",
                ("Location: https://127.0.0.1/private",),
                b"",
            ),
        }
        with EnterpriseProxy(cert_path, key_path, responses) as proxy:
            old_env = _configure_proxy(proxy.url)
            try:
                os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "1"
                err, body, _content_type = tools_mod._fetch_url_raw(
                    "https://enterprise-proxy.test/start"
                )
            finally:
                _restore_env(old_env)
            assert err == "Blocked: refusing to fetch non-public address 127.0.0.1."
            assert body == ""
            assert resolver_calls == [
                ("enterprise-proxy.test", 443),
                ("127.0.0.1", 443),
            ]
            assert _connect_targets(proxy) == ["enterprise-proxy.test:443"]
            assert _host_headers(proxy) == ["Host: enterprise-proxy.test"]

        resolver_calls.clear()
        with EnterpriseProxy(cert_path, key_path) as proxy:
            old_env = _configure_proxy(proxy.url)
            try:
                os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "1"
                err, body, _content_type = tools_mod._fetch_url_raw(
                    "https://user:pass@enterprise-proxy.test:8443/credentials"
                )
            finally:
                _restore_env(old_env)
            assert err is None, err
            assert body == "ok"
            assert _connect_targets(proxy) == ["enterprise-proxy.test:8443"], (
                "URL userinfo leaked into CONNECT",
                _connect_targets(proxy),
            )
            origin_headers = proxy.server.origin_requests[0][1].splitlines()
            assert not any(line.lower().startswith("authorization:") for line in origin_headers)

        resolver_calls.clear()
        with EnterpriseHTTPProxy() as proxy:
            old_env = _configure_proxy(proxy.url, target_scheme="http")
            try:
                os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "0"
                err, body, _content_type = tools_mod._fetch_url_raw(
                    "http://enterprise-proxy.test:8080/page?q=1"
                )
            finally:
                _restore_env(old_env)
            assert err and "403" in err, (err, body)
            request = proxy.server.requests[0]
            assert request.splitlines()[0] == (
                f"GET http://{PINNED_IP}:8080/page?q=1 HTTP/1.1"
            )
            assert "Host: enterprise-proxy.test:8080" in request.splitlines()

        resolver_calls.clear()
        with EnterpriseHTTPProxy() as proxy:
            old_env = _configure_proxy(proxy.url, target_scheme="http")
            try:
                os.environ["UNSLOTH_STUDIO_DISABLE_DNS_PINNING"] = "1"
                err, body, content_type = tools_mod._fetch_url_raw(
                    "http://enterprise-proxy.test:8080/page?q=1"
                )
            finally:
                _restore_env(old_env)
            assert err is None, err
            assert body == "http-proxy-ok"
            assert content_type == "text/plain"
            request = proxy.server.requests[0]
            assert request.splitlines()[0] == (
                "GET http://enterprise-proxy.test:8080/page?q=1 HTTP/1.1"
            )
            assert "Host: enterprise-proxy.test:8080" in request.splitlines()

    print(f"PASS platform={sys.platform} python={sys.version.split()[0]}")
    print("PASS default mode sends a pinned-IP CONNECT and is rejected by hostname-only proxy")
    print("PASS opt-out sends hostname CONNECT, correct SNI, explicit Host port, and fetches")
    print("PASS redirects revalidate and preserve hostname CONNECT, SNI, and Host ports")
    print("PASS redirects to loopback remain blocked before a second proxy connection")
    print("PASS URL userinfo is not leaked into CONNECT or origin Authorization headers")
    print("PASS HTTP proxies receive pinned IPs by default and hostnames with the opt-out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
