"""Instance registry - maps file_id to connection/handler."""
import threading


class FileEntry:
    __slots__ = ('fid', 'name', 'arch', 'bits', 'path', 'pid', 'conn', 'local', 'call_port')

    def __init__(self, fid, name, arch, bits, path, pid, conn=None, local=False, call_port=0):
        self.fid = fid
        self.name = name
        self.arch = arch
        self.bits = bits
        self.path = path
        self.pid = pid
        self.conn = conn  # control socket for remote workers
        self.local = local
        self.call_port = call_port  # worker's dedicated call-handling port


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._files = {}  # fid -> FileEntry
        self._conns = {}  # socket -> [fid, ...]

    def register(self, entry):
        with self._lock:
            self._files[entry.fid] = entry
            if entry.conn:
                self._conns.setdefault(id(entry.conn), []).append(entry.fid)

    def unregister(self, fid):
        with self._lock:
            entry = self._files.pop(fid, None)
            if entry and entry.conn:
                cl = self._conns.get(id(entry.conn), [])
                if fid in cl:
                    cl.remove(fid)
            return entry

    def unregister_conn(self, conn):
        """Remove all files associated with a connection."""
        with self._lock:
            fids = self._conns.pop(id(conn), [])
            for fid in fids:
                self._files.pop(fid, None)
            return fids

    def get(self, fid):
        with self._lock:
            return self._files.get(fid)

    def list_all(self):
        with self._lock:
            return list(self._files.values())

    def has(self, fid):
        with self._lock:
            return fid in self._files

    def count(self):
        with self._lock:
            return len(self._files)

    def all_conns(self):
        """Return unique remote connections."""
        with self._lock:
            seen = set()
            conns = []
            for e in self._files.values():
                if e.conn and id(e.conn) not in seen:
                    seen.add(id(e.conn))
                    conns.append(e.conn)
            return conns

    def remote_entries(self):
        with self._lock:
            return [e for e in self._files.values() if not e.local]
