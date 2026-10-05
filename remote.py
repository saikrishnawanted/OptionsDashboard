"""Explicit HTTPS origin and authenticated loopback proxy for remote deployment."""
import ipaddress
import secrets
from urllib.parse import urlsplit


class RemoteAccess:
    def __init__(self, origin='', proxy_token=''):
        self.origin = origin.rstrip('/')
        self.proxy_token = proxy_token
        self.host = None
        if self.origin:
            parsed = urlsplit(self.origin)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment or len(proxy_token) < 32):
                raise ValueError('Remote deployment requires an HTTPS origin and a proxy token of at least 32 characters.')
            self.host = parsed.hostname

    def allowed_origin(self, origin, host):
        if self.origin:
            return origin == self.origin and host == urlsplit(self.origin).netloc
        return origin == f'http://{host}' and host.split(':')[0] in ('127.0.0.1', 'localhost', 'testserver')

    def authorized_proxy(self, headers, client_host):
        if not self.origin:
            return True
        try:
            loopback = ipaddress.ip_address(client_host).is_loopback
        except (ValueError, TypeError):
            loopback = client_host == 'testclient'
        return loopback and secrets.compare_digest(headers.get('x-terminal-proxy-token', ''), self.proxy_token)
