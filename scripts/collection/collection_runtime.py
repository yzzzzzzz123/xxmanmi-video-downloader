"""Server collection settings and the durable retirement of local writes."""
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
LOCAL_RETIRED = PROJECT / '.runtime/collection-server/local-disabled.json'

def production():
    mode = os.environ.get('MANYING_COLLECTION_MODE', 'local')
    if mode not in {'local', 'production'}:
        raise ValueError('Unknown collection mode')
    if mode == 'production' and sys.platform != 'linux':
        raise RuntimeError('Production collection runs only on the server')
    return mode == 'production'

def require_write():
    if not production() and LOCAL_RETIRED.exists():
        raise RuntimeError('Local collection is retired; resume the server collector instead')

def configuration():
    if not production():
        raise RuntimeError('Explicit server production mode required')
    path = Path(os.environ.get('MANYING_COLLECTION_CONFIG', '/etc/manying-collector/collector.json'))
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('Expected a fixed server configuration file')
    value = json.loads(path.read_text())
    if (value.get('database') != 'manying' or value.get('host') != '127.0.0.1'
            or value.get('port') != 5432 or value.get('user') != 'manying_collector'
            or value.get('mode') != 'production' or value.get('network') != 'native-direct'):
        raise ValueError('Unexpected production collection target')
    return value

def database_options(*, stage=False, write=False):
    config = configuration()
    secret = Path(config['password_file'])
    if not secret.is_absolute() or secret.is_symlink() or secret.stat().st_mode & 0o077:
        raise ValueError('Database credential must be a private server file')
    schema = 'collector_stage,public' if stage else 'public'
    return dict(host=config['host'], port=config['port'], dbname=config['database'],
        user=config['user'], password=secret.read_text().strip(), connect_timeout=5,
        options=f'-c search_path={schema} -c default_transaction_read_only={"off" if write else "on"} -c statement_timeout=45000 -c lock_timeout=5000',
        application_name='manying-server-collector')

def native_route(address='1.1.1.1'):
    configuration()
    result = subprocess.run(['ip','-json','route','get',str(address)],
        capture_output=True, text=True, check=True, timeout=5)
    routes = json.loads(result.stdout)
    device = routes[0].get('dev', '') if len(routes) == 1 else ''
    if not device or device.startswith(('lo','tun','tap','wg','ppp','clash','mihomo')):
        raise RuntimeError('Native physical server route was not established')
    return {'mode':'native-direct', 'device':device}
