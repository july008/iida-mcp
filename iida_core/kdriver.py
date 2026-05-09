"""Out-of-process client for iida-mcp-ioctl kernel driver communication."""
import json
import os
import subprocess

_DRIVER_NOT_LOADED = {'e': 'kernel driver iida-mcp-ioctl not loaded'}


def _helper_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'iida-kdrv-helper.exe')


def _run_helper(args):
    helper = _helper_path()
    if not os.path.exists(helper):
        return {'e': f'kernel helper missing: {helper}'}
    try:
        proc = subprocess.run(
            [helper] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3.0,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    except subprocess.TimeoutExpired:
        return {'e': 'kernel helper timeout'}
    except OSError as e:
        return {'e': f'kernel helper launch failed: {e}'}
    except Exception as e:
        return {'e': f'kernel helper exception: {e}'}

    out = proc.stdout.decode('utf-8', errors='replace').strip()
    if not out:
        err = proc.stderr.decode('utf-8', errors='replace').strip()
        return {'e': f'kernel helper exited {proc.returncode}: {err}'}
    try:
        return json.loads(out)
    except Exception:
        return {'e': f'bad helper output: {out[:200]}'}


def read_kernel_memory(address, size):
    if size <= 0:
        return {'e': 'size must be > 0'}
    if size > 65536:
        return {'e': 'size too large (max 65536)'}
    result = _run_helper(['read', format(address, 'x'), str(size)])
    if isinstance(result, dict):
        if result.get('e') == 'kernel driver iida-mcp-ioctl not loaded':
            return _DRIVER_NOT_LOADED
        return result
    if not result:
        return {'e': f'read returned 0 bytes at {address:x}'}
    return result


def get_module_list():
    return _run_helper(['modules'])


def get_module_base(name):
    return _run_helper(['base', name])
