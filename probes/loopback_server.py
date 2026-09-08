# SPDX-License-Identifier: AGPL-3.0-only
import socketserver
from http.server import ThreadingHTTPServer

class LoopbackHTTPServer(ThreadingHTTPServer):
    """Numeric loopback binding needs no reverse-DNS lookup for a display name."""
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
