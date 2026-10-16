#!/usr/bin/env python3
"""Loopback TLS CONNECT relay plus a hidden SSH reverse tunnel; no TLS interception."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

PORT = 17893
REMOTE_PORT = 17892
DESKTOP_PORT = 16087
SUFFIXES = ('qq.com','qpic.cn','qlogo.cn','gtimg.com','weixin.com','weixinbridge.com')
PROBES = {'myip.ipip.net'}
INTERFACE = None
_source_cache = (0, None)


def interface_socket(kind):
    """Bind only this relay's sockets to the selected physical Windows NIC."""
    global _source_cache
    if os.name != 'nt' or not INTERFACE:
        raise OSError('Physical interface is not configured')
    if time.monotonic() - _source_cache[0] > 60 or not _source_cache[1]:
        command = (f"Get-NetIPAddress -InterfaceIndex {int(INTERFACE)} -AddressFamily IPv4 | "
                   "Where-Object {$_.AddressState -eq 'Preferred'} | "
                   "Select-Object -ExpandProperty IPAddress")
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=12, **hidden())
        addresses = [s.strip() for s in result.stdout.decode().splitlines() if s.strip()]
        addresses = [s for s in addresses if ipaddress.IPv4Address(s).is_private
                     and not ipaddress.IPv4Address(s).is_link_local]
        if result.returncode or len(addresses) != 1:
            raise OSError('Domestic physical interface has no unique IPv4 address')
        _source_cache = (time.monotonic(), addresses[0])
    connection = socket.socket(socket.AF_INET, kind)
    try:
        # Windows IP_UNICAST_IF takes a DWORD interface index in network byte order.
        connection.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(INTERFACE))
        connection.bind((_source_cache[1], 0))
        connection.settimeout(12)
        return connection
    except Exception:
        connection.close()
        raise


def dns_answers(data, identifier):
    if len(data) < 12:
        raise OSError('Truncated DNS response')
    ident, flags, questions, answers, _, _ = struct.unpack_from('!6H', data)
    if ident != identifier or not flags & 0x8000 or flags & 0x020F or questions != 1:
        raise OSError('Invalid DNS response')
    def skip_name(pos):
        for _ in range(128):
            length = data[pos]
            if length & 0xC0 == 0xC0:
                return pos + 2
            if length == 0:
                return pos + 1
            if length > 63:
                raise OSError('Invalid DNS name')
            pos += length + 1
        raise OSError('DNS name too long')
    pos = skip_name(12) + 4
    result = []
    for _ in range(answers):
        pos = skip_name(pos)
        kind, cls, _, length = struct.unpack_from('!HHIH', data, pos)
        pos += 10
        value = data[pos:pos + length]
        if len(value) != length:
            raise OSError('Truncated DNS answer')
        if kind == 1 and cls == 1 and length == 4:
            address = socket.inet_ntoa(value)
            if ipaddress.IPv4Address(address).is_global:
                result.append(address)
        pos += length
    return result


def physical_addresses(host):
    identifier = secrets.randbelow(65536)
    name = b''.join(bytes([len(p)]) + p.encode('ascii') for p in host.split('.')) + b'\0'
    query = struct.pack('!6H', identifier, 0x0100, 1, 0, 0, 0) + name + struct.pack('!HH', 1, 1)
    for resolver in ('223.5.5.5', '119.29.29.29'):
        try:
            with interface_socket(socket.SOCK_DGRAM) as connection:
                connection.connect((resolver, 53))
                connection.send(query)
                addresses = dns_answers(connection.recv(4096), identifier)
                if addresses:
                    return addresses
        except (OSError, IndexError, struct.error):
            continue
    raise OSError('Physical-interface DNS unavailable; no proxy fallback')


def allowed(host, port):
    host = host.lower().rstrip('.')
    return port == 443 and (host in PROBES or any(host == s or host.endswith('.'+s) for s in SUFFIXES))


def public_connection(host, port):
    if INTERFACE:
        for address in physical_addresses(host):
            connection = interface_socket(socket.SOCK_STREAM)
            try:
                connection.connect((address, port))
                return connection
            except OSError:
                connection.close()
        raise OSError('Domestic connection unavailable; no proxy fallback')
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for family, kind, protocol, _, address in rows:
        ip = ipaddress.ip_address(address[0])
        # This machine's existing TUN DNS returns synthetic 198.18/15 addresses.
        # Let its existing domain mapping route these approved hosts; never admit LAN/loopback.
        tun_fake = os.name == 'nt' and ip.version == 4 and ip in ipaddress.ip_network('198.18.0.0/15')
        if not ip.is_global and not tun_fake:
            continue
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(20)
        try:
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise OSError('No reachable public address')


def ssh_transport():
    """OpenSSH ProxyCommand transport to this deployment only; SSH keeps its encryption."""
    if not INTERFACE:
        raise ValueError('A domestic physical interface is required')
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    with interface_socket(socket.SOCK_STREAM) as connection:
        connection.connect(('43.134.92.99', 22))
        connection.settimeout(None)
        def send():
            try:
                while True:
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        connection.shutdown(socket.SHUT_WR)
                        return
                    connection.sendall(data)
            except OSError:
                return
        threading.Thread(target=send, daemon=True).start()
        while True:
            data = connection.recv(65536)
            if not data:
                return
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


def ssh_proxy_option(interface):
    transport = subprocess.list2cmdline([sys.executable, '-B', str(Path(__file__).resolve()),
                                       'ssh-transport', '--interface', str(interface)])
    return ['-o', 'ProxyCommand=' + transport]


class Relay(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path != '/health':
            self.send_error(405)
            return
        value = {'service':'manying-china-relay','instance':self.server.instance,
                 'interface':INTERFACE,'remote_port':REMOTE_PORT,'desktop_port':DESKTOP_PORT,
                 'pid':os.getpid(),'ssh_pid':self.server.ssh.pid if self.server.ssh else None,
                 'ssh_process_running':bool(self.server.ssh and self.server.ssh.poll() is None)}
        data=json.dumps(value).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

    def do_POST(self):
        if self.path != '/stop' or self.headers.get('X-Manying-Control') != self.server.control:
            self.send_error(403)
            return
        self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        threading.Thread(target=self.server.shutdown,daemon=True).start()

    def do_CONNECT(self):
        try:
            host, raw_port = self.path.rsplit(':',1)
            port=int(raw_port)
            if not allowed(host,port):
                self.send_error(403)
                return
            upstream=public_connection(host.lower().rstrip('.'),port)
        except (OSError,ValueError):
            self.send_error(502)
            return
        self.send_response(200,'Connection Established');self.end_headers()
        with upstream:
            self.connection.settimeout(30)
            peers=[self.connection,upstream]
            try:
                while True:
                    ready,_,_=select.select(peers,[],[],120)
                    if not ready:return
                    for source in ready:
                        data=source.recv(65536)
                        if not data:return
                        (upstream if source is self.connection else self.connection).sendall(data)
            except OSError:
                return


def state_path():
    name='china-relay.json' if PORT == 17893 else f'china-relay-{PORT}.json'
    return Path(__file__).resolve().parents[3]/'.runtime/publisher-server'/name


def health():
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{PORT}/health',timeout=2) as response:
            return json.load(response)
    except (OSError,ValueError):return None


def hidden():
    if os.name == 'nt':
        info=subprocess.STARTUPINFO();info.dwFlags|=subprocess.STARTF_USESHOWWINDOW;info.wShowWindow=subprocess.SW_HIDE
        return dict(creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=info)
    return dict(start_new_session=True)


class ChildJob:
    """Windows kills this relay's SSH descendants if Task Scheduler stops the parent."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':return
        import ctypes
        from ctypes import wintypes as w
        class Limits(ctypes.Structure):
            _fields_ = [('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',w.DWORD),
                        ('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('active',w.DWORD),
                        ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('reads','writes','other','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic',Limits),('io',IO),('process_memory',ctypes.c_size_t),
                        ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.api.CreateJobObjectW.restype=w.HANDLE
        self.api.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.api.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        self.api.CloseHandle.argtypes=[w.HANDLE]
        self.handle=self.api.CreateJobObjectW(None,None)
        info=Extended();info.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close();raise ctypes.WinError(ctypes.get_last_error())

    def assign(self,process):
        if self.handle and not self.api.AssignProcessToJobObject(self.handle,int(process._handle)):
            import ctypes
            process.terminate();process.wait(timeout=5)
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None


def serve():
    os.umask(0o077)
    root=state_path().parent;root.mkdir(parents=True,exist_ok=True)
    server=Relay(('127.0.0.1',PORT),Handler)
    server.instance=secrets.token_hex(12);server.control=secrets.token_hex(24);server.ssh=None
    state_path().write_text(json.dumps({'instance':server.instance,'control':server.control,'pid':os.getpid()}),encoding='utf-8')
    stop=threading.Event()
    children=ChildJob()
    fatal=[]
    key=Path.home()/'.ssh/manying_deploy_20260929'
    command=['ssh','-T','-N','-i',str(key),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
             '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=20','-o','ServerAliveCountMax=3',
             '-o','ConnectTimeout=10','-R',f'127.0.0.1:{REMOTE_PORT}:127.0.0.1:{PORT}',
             '-L',f'127.0.0.1:{DESKTOP_PORT}:127.0.0.1:6087','ubuntu@43.134.92.99']
    if INTERFACE:
        command[1:1] = ssh_proxy_option(INTERFACE)
    def tunnel():
        with (root/'china-tunnel.log').open('ab') as log:
            while not stop.is_set():
                try:
                    server.ssh=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,**hidden())
                    children.assign(server.ssh)
                except Exception as error:
                    fatal.append(type(error).__name__)
                    server.shutdown()
                    return
                while not stop.wait(1) and server.ssh.poll() is None:pass
                if stop.is_set():break
                stop.wait(30)
    thread=threading.Thread(target=tunnel,daemon=True);thread.start()
    try:server.serve_forever(poll_interval=.3)
    finally:
        stop.set()
        if server.ssh and server.ssh.poll() is None:
            server.ssh.terminate()
            try:server.ssh.wait(timeout=5)
            except subprocess.TimeoutExpired:server.ssh.kill();server.ssh.wait()
        thread.join(6);children.close();server.server_close()
    if fatal:raise RuntimeError('Publisher tunnel supervisor failed: '+fatal[0])


def main():
    global PORT, REMOTE_PORT, INTERFACE, DESKTOP_PORT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('start','serve','status','stop','ssh-transport'))
    parser.add_argument('--port',type=int,default=17893)
    parser.add_argument('--remote-port',type=int,default=17892)
    parser.add_argument('--interface',type=int,help='Domestic physical Windows interface index; leaves TUN/mode unchanged')
    parser.add_argument('--desktop-port',type=int,default=16087)
    args=parser.parse_args()
    if not all(1024 <= p <= 65535 for p in (args.port,args.remote_port,args.desktop_port)):
        parser.error('Use unprivileged dedicated ports')
    PORT,REMOTE_PORT=args.port,args.remote_port
    DESKTOP_PORT=args.desktop_port
    INTERFACE=args.interface
    if INTERFACE is not None and (os.name != 'nt' or INTERFACE <= 0):
        parser.error('Use a positive physical Windows interface index')
    if args.operation == 'ssh-transport':ssh_transport();return
    if args.operation=='serve':serve();return
    current=health()
    if args.operation=='status':
        print(json.dumps(current or {'state':'offline'}));return
    if args.operation=='stop':
        record=json.loads(state_path().read_text(encoding='utf-8'))
        if not current or current.get('instance')!=record['instance']:raise RuntimeError('Unconfirmed relay identity')
        request=urllib.request.Request(f'http://127.0.0.1:{PORT}/stop',method='POST',headers={'X-Manying-Control':record['control']})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=3):pass
        print(json.dumps({'state':'stopping'}));return
    if current:
        if current.get('service')!='manying-china-relay':raise RuntimeError('Port belongs to another service')
        if (current.get('interface') != INTERFACE or current.get('remote_port') != REMOTE_PORT
                or current.get('desktop_port') != DESKTOP_PORT):
            raise RuntimeError('Existing relay has different routing; preserve it and choose another port')
        print(json.dumps(current));return
    root=state_path().parent;root.mkdir(parents=True,exist_ok=True)
    with (root/'china-relay.log').open('ab') as log:
        child=subprocess.Popen([sys.executable,'-X','utf8','-u','-B',str(Path(__file__).resolve()),'serve',
                                '--port',str(PORT),'--remote-port',str(REMOTE_PORT),'--desktop-port',str(DESKTOP_PORT),
                                *(['--interface',str(INTERFACE)] if INTERFACE else [])],
                               stdin=subprocess.DEVNULL,stdout=log,stderr=log,**hidden())
    for _ in range(30):
        current=health()
        if current:print(json.dumps(current));return
        if child.poll() is not None:raise RuntimeError('Relay failed; inspect its private log')
        time.sleep(.2)
    raise RuntimeError('Relay startup was not confirmed')


if __name__=='__main__':main()
