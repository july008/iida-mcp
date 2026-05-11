"""Lightweight HTTP MCP server - implements MCP JSON-RPC over Streamable HTTP."""
import json
import threading
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

from . import protocol
from .registry import Registry, FileEntry
from .router import Router


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread to avoid blocking."""
    daemon_threads = True

MCP_PORT = 13897
INTERNAL_PORT = 13898  # internal worker connections

SERVER_INFO = {
    "name": "iida-mcp",
    "version": "0.3.0"
}

CAPABILITIES = {
    "tools": {},
    "resources": {}
}


class McpHandler(BaseHTTPRequestHandler):
    """Handle MCP Streamable HTTP requests."""

    def log_message(self, format, *args):
        pass  # silence logs

    def do_POST(self):
        if self.path != '/mcp':
            self._err(404, 'not found')
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''

        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._jsonrpc_error(None, -32700, 'parse error')
            return

        method = req.get('method', '')
        rid = req.get('id')
        params = req.get('params', {})

        if method == 'initialize':
            self._jsonrpc_ok(rid, {
                "protocolVersion": "2025-03-26",
                "serverInfo": SERVER_INFO,
                "capabilities": CAPABILITIES
            })
        elif method.startswith('notifications/'):
            # Notifications have no id, return 202 Accepted
            self._send(202, b'', 'application/json')
        elif method == 'tools/list':
            tools = self.server.mcp_server.get_tools_list()
            self._jsonrpc_ok(rid, {"tools": tools})
        elif method == 'tools/call':
            name = params.get('name', '')
            args = params.get('arguments', {})
            try:
                result = self.server.mcp_server.handle_tool_call(name, args)
                self._jsonrpc_ok(rid, {
                    "content": [{"type": "text", "text": json.dumps(result, separators=(',', ':'))}]
                })
            except Exception as e:
                self._jsonrpc_ok(rid, {
                    "content": [{"type": "text", "text": json.dumps({"e": str(e)}, separators=(',', ':'))}]
                })
        elif method == 'resources/list':
            resources = self.server.mcp_server.get_resources_list()
            self._jsonrpc_ok(rid, {"resources": resources})
        elif method == 'resources/templates/list':
            templates = self.server.mcp_server.get_resource_templates_list()
            self._jsonrpc_ok(rid, {"resourceTemplates": templates})
        elif method == 'resources/read':
            uri = params.get('uri', '')
            try:
                content = self.server.mcp_server.read_resource(uri)
                self._jsonrpc_ok(rid, {"contents": [content]})
            except Exception as e:
                self._jsonrpc_error(rid, -32602, str(e))
        elif method == 'prompts/list':
            self._jsonrpc_ok(rid, {"prompts": []})
        elif method == 'ping':
            self._jsonrpc_ok(rid, {})
        else:
            self._jsonrpc_error(rid, -32601, 'method not found')

    def do_GET(self):
        if self.path == '/mcp':
            self._send(405, b'use POST', 'text/plain')
        else:
            self._err(404, 'not found')

    def do_DELETE(self):
        self._send(200, b'', 'text/plain')

    def _jsonrpc_ok(self, rid, result):
        resp = {"jsonrpc": "2.0", "id": rid, "result": result}
        self._send_json(resp)

    def _jsonrpc_error(self, rid, code, msg):
        resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}
        self._send_json(resp)

    def _send_json(self, obj):
        data = json.dumps(obj, separators=(',', ':')).encode('utf-8')
        self._send(200, data, 'application/json')

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, msg):
        self._send(code, msg.encode(), 'text/plain')


class McpServer:
    """Master MCP server managing registry, router, and HTTP."""

    def __init__(self, tools_module, local_handler):
        self.registry = Registry()
        self.router = Router(self.registry, local_handler)
        self.tools_module = tools_module
        self._http = None
        self._http_thread = None
        self._internal_sock = None
        self._internal_thread = None
        self._running = False

    def start(self):
        self._running = True
        self._start_http()
        self._start_internal()

    def stop(self):
        self._running = False
        if self._http:
            self._http.shutdown()
        if self._internal_sock:
            try:
                self._internal_sock.close()
            except:
                pass
        self._notify_workers_shutdown()

    def _start_http(self):
        self._http = ThreadedHTTPServer(('127.0.0.1', MCP_PORT), McpHandler)
        self._http.mcp_server = self
        self._http_thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._http_thread.start()

    def _start_internal(self):
        self._internal_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._internal_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._internal_sock.bind(('127.0.0.1', INTERNAL_PORT))
        self._internal_sock.listen(16)
        self._internal_thread = threading.Thread(target=self._accept_workers, daemon=True)
        self._internal_thread.start()

    def _accept_workers(self):
        while self._running:
            try:
                conn, addr = self._internal_sock.accept()
                t = threading.Thread(target=self._handle_worker, args=(conn,), daemon=True)
                t.start()
            except OSError:
                break

    def _handle_worker(self, conn):
        """Handle a single worker connection."""
        try:
            while self._running:
                msg = protocol.recv_msg(conn)
                if msg is None:
                    break
                mt = msg.get('t')
                if mt == protocol.MSG_REGISTER:
                    entry = FileEntry(
                        fid=msg['fid'],
                        name=msg['name'],
                        arch=msg['arch'],
                        bits=msg['bits'],
                        path=msg['path'],
                        pid=msg['pid'],
                        conn=conn,
                        local=False,
                        call_port=msg.get('call_port', 0)
                    )
                    self.registry.register(entry)
                    protocol.send_msg(conn, {'t': protocol.MSG_ACK, 'ok': True})
                elif mt == protocol.MSG_UNREGISTER:
                    self.registry.unregister(msg['fid'])
                    protocol.send_msg(conn, {'t': protocol.MSG_ACK, 'ok': True})
                elif mt == protocol.MSG_HEARTBEAT:
                    protocol.send_msg(conn, {'t': protocol.MSG_HEARTBEAT})
                elif mt == protocol.MSG_RESULT:
                    pass  # handled inline by router
        except Exception:
            pass
        finally:
            self.registry.unregister_conn(conn)
            try:
                conn.close()
            except:
                pass

    def _notify_workers_shutdown(self):
        """Notify all workers to elect a new master."""
        for conn in self.registry.all_conns():
            try:
                protocol.send_msg(conn, {'t': protocol.MSG_PROMOTE})
            except:
                pass

    def get_tools_list(self):
        return self.tools_module.TOOLS_SCHEMA

    def get_resources_list(self):
        resources = [
            {
                "uri": "iida://files",
                "name": "Connected IDA files",
                "description": "JSON list of IDA databases currently registered with iida-mcp.",
                "mimeType": "application/json"
            },
            {
                "uri": "iida://tools",
                "name": "iida-mcp tool catalog",
                "description": "JSON MCP tool schemas exposed by iida-mcp.",
                "mimeType": "application/json"
            }
        ]
        for entry in self.registry.list_all():
            resources.append({
                "uri": "iida://file/{}/summary".format(entry.fid),
                "name": "{} ({})".format(entry.name, entry.fid),
                "description": "Connected IDA file summary.",
                "mimeType": "application/json"
            })
        return resources

    def get_resource_templates_list(self):
        return [
            {
                "uriTemplate": "iida://file/{f}/info",
                "name": "IDA file info",
                "description": "Detailed IDB metadata for a connected file_id.",
                "mimeType": "application/json"
            },
            {
                "uriTemplate": "iida://file/{f}/functions",
                "name": "IDA functions",
                "description": "First page of functions for a connected file_id.",
                "mimeType": "application/json"
            }
        ]

    def read_resource(self, uri):
        if uri == "iida://files":
            data = self.router.dispatch('list_files', {})
            return _json_resource(uri, data)
        if uri == "iida://tools":
            return _json_resource(uri, self.tools_module.TOOLS_SCHEMA)

        parsed = urlparse(uri)
        if parsed.scheme != "iida" or parsed.netloc != "file":
            raise ValueError("unknown resource uri")

        parts = [p for p in parsed.path.split('/') if p]
        if len(parts) != 2:
            raise ValueError("unknown resource uri")

        fid, kind = parts
        entry = self.registry.get(fid)
        if not entry:
            raise ValueError("unknown file_id")

        if kind == "summary":
            data = {
                "fid": entry.fid,
                "name": entry.name,
                "arch": entry.arch,
                "bits": entry.bits,
                "path": entry.path,
                "pid": entry.pid,
                "local": entry.local
            }
            return _json_resource(uri, data)
        if kind == "info":
            return _json_resource(uri, self.router.dispatch('get_info', {'f': fid}))
        if kind == "functions":
            return _json_resource(uri, self.router.dispatch('list_functions', {'f': fid, 'n': 100}))

        raise ValueError("unknown resource uri")

    def handle_tool_call(self, name, args):
        return self.router.dispatch(name, args)


def _json_resource(uri, data):
    return {
        "uri": uri,
        "mimeType": "application/json",
        "text": json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    }


def try_bind_master():
    """Check if we should become master by trying to connect to existing master.
    Returns True if no master exists (we should become master)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(('127.0.0.1', INTERNAL_PORT))
        s.close()
        return False  # connected => master exists, we are worker
    except (ConnectionRefusedError, OSError, TimeoutError):
        return True  # no master, we become master
