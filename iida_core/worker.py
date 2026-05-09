"""Worker node - connects to master and handles forwarded tool calls."""
import socket
import threading
import time
import os

from . import protocol
from .server import INTERNAL_PORT, MCP_PORT, McpServer


class Worker:
    """Runs inside a non-master IDA instance."""

    def __init__(self, file_info, local_handler):
        """
        file_info: dict with fid, name, arch, bits, path
        local_handler: function(tool, args) -> result
        """
        self.file_info = file_info
        self.local_handler = local_handler
        self._conn = None
        self._running = False
        self._thread = None
        self._promoted = False
        self._master_server = None
        self._call_port = 0
        self._call_sock = None

    def start(self):
        self._running = True
        self._start_call_server()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._conn:
            try:
                protocol.send_msg(self._conn, {
                    't': protocol.MSG_UNREGISTER,
                    'fid': self.file_info['fid']
                })
            except:
                pass
            try:
                self._conn.close()
            except:
                pass
        if self._call_sock:
            try:
                self._call_sock.close()
            except:
                pass

    def is_promoted(self):
        return self._promoted

    def get_master_server(self):
        return self._master_server

    def _start_call_server(self):
        """Start a TCP server on a random port to handle tool calls from master."""
        self._call_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._call_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._call_sock.bind(('127.0.0.1', 0))  # OS picks a free port
        self._call_port = self._call_sock.getsockname()[1]
        self._call_sock.listen(16)
        t = threading.Thread(target=self._accept_calls, daemon=True)
        t.start()

    def _accept_calls(self):
        """Accept incoming call connections from master's router."""
        while self._running:
            try:
                conn, addr = self._call_sock.accept()
                t = threading.Thread(target=self._handle_call_conn, args=(conn,), daemon=True)
                t.start()
            except OSError:
                break

    def _handle_call_conn(self, conn):
        """Handle a single call request on a dedicated connection."""
        try:
            msg = protocol.recv_msg(conn)
            if msg and msg.get('t') == protocol.MSG_CALL:
                result = self._handle_call(msg)
                protocol.send_msg(conn, {'t': protocol.MSG_RESULT, 'r': result})
        except:
            pass
        finally:
            try:
                conn.close()
            except:
                pass

    def _run(self):
        while self._running:
            try:
                self._conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._conn.connect(('127.0.0.1', INTERNAL_PORT))
                self._register()
                self._loop()
            except (ConnectionRefusedError, OSError):
                if not self._running:
                    break
                time.sleep(0.5)
                if self._try_become_master():
                    break
            except Exception:
                if not self._running:
                    break
                time.sleep(1.0)

    def _register(self):
        protocol.send_msg(self._conn, {
            't': protocol.MSG_REGISTER,
            'fid': self.file_info['fid'],
            'name': self.file_info['name'],
            'arch': self.file_info['arch'],
            'bits': self.file_info['bits'],
            'path': self.file_info['path'],
            'pid': os.getpid(),
            'call_port': self._call_port
        })
        ack = protocol.recv_msg(self._conn)
        if not ack or not ack.get('ok'):
            raise RuntimeError('register failed')

    def _loop(self):
        """Control connection loop - only handles control messages (promote, heartbeat)."""
        while self._running:
            msg = protocol.recv_msg(self._conn)
            if msg is None:
                self._try_become_master()
                break

            mt = msg.get('t')
            if mt == protocol.MSG_PROMOTE:
                self._try_become_master()
                break
            elif mt == protocol.MSG_HEARTBEAT:
                try:
                    protocol.send_msg(self._conn, {'t': protocol.MSG_HEARTBEAT})
                except:
                    break

    def _handle_call(self, msg):
        tool = msg.get('tool', '')
        args = msg.get('args', {})
        try:
            return self.local_handler(tool, args)
        except Exception as ex:
            return {'e': str(ex)}

    def _try_become_master(self):
        """Attempt to take over as master."""
        from . import tools as tools_module
        try:
            if self._conn:
                self._conn.close()
                self._conn = None

            time.sleep(0.2)

            if not _port_available(MCP_PORT):
                return False

            self._master_server = McpServer(tools_module, self.local_handler)
            from .registry import FileEntry
            entry = FileEntry(
                fid=self.file_info['fid'],
                name=self.file_info['name'],
                arch=self.file_info['arch'],
                bits=self.file_info['bits'],
                path=self.file_info['path'],
                pid=os.getpid(),
                conn=None,
                local=True
            )
            self._master_server.registry.register(entry)
            self._master_server.start()
            self._promoted = True
            return True
        except Exception:
            return False


def _port_available(port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(('127.0.0.1', port))
        s.close()
        return False  # someone is listening
    except (ConnectionRefusedError, OSError, TimeoutError):
        return True
