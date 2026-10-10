import socket,struct,os,json,base64,hashlib,time,subprocess,queue,threading
import shutil
from pathlib import Path

def codex_binary():
 configured = os.environ.get('XXMANMI_CODEX_BIN')
 if configured:
  return str(Path(configured).expanduser())
 direct = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
 if direct:
  return direct
 roots = []
 wrapper = shutil.which('codex')
 if wrapper:
  roots.append(Path(wrapper).parent / 'node_modules/@openai/codex')
 if os.name == 'nt':
  roaming = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming')))
  roots.append(roaming / 'npm/node_modules/@openai/codex')
 for root in roots:
  candidates = sorted(root.glob('node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe'))
  if candidates:
   return str(candidates[0])
 return 'codex.exe' if os.name == 'nt' else 'codex'

CODEX=codex_binary()
CODEX_HOME_PATH=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
SOCK=os.environ.get('XXMANMI_CODEX_SOCKET', str(CODEX_HOME_PATH / 'app-server-control/app-server-control.sock'))

def hidden_options():
 if os.name != 'nt':
  return {'start_new_session': True}
 info=subprocess.STARTUPINFO()
 info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
 info.wShowWindow=subprocess.SW_HIDE
 return {'creationflags': subprocess.CREATE_NO_WINDOW, 'startupinfo': info}

class Proxy:
 def __init__(self):
  self.p=subprocess.Popen([CODEX,'app-server','proxy','--sock',SOCK],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**hidden_options())
  self.q=queue.Queue();self.buf=b''
  def read():
   while True:
    b=os.read(self.p.stdout.fileno(),65536);self.q.put(b)
    if not b:break
  threading.Thread(target=read,daemon=True).start()
 def sendall(self,p):self.p.stdin.write(p);self.p.stdin.flush()
 def recv(self,n):
  if not self.buf:self.buf=self.q.get(timeout=15)
  b,self.buf=self.buf[:n],self.buf[n:];return b
 def close(self):
  self.p.stdin.close()
  try:self.p.wait(timeout=3)
  except subprocess.TimeoutExpired:self.p.terminate();self.p.wait(timeout=3)

class RPC:
 def __enter__(self):
  self.s=Proxy()
  key=base64.b64encode(os.urandom(16)).decode()
  self.s.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '+key+'\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
  h=b''
  while not h.endswith(b'\r\n\r\n'):h+=self.s.recv(1)
  if b' 101 ' not in h:raise RuntimeError('WebSocket handshake rejected: '+h.split(b'\r\n')[0].decode())
  expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
  if expected not in h:raise RuntimeError('WebSocket handshake accept mismatch')
  self.i=0
  self.call('initialize',{'clientInfo':{'name':'xxmanmi_main_resume','version':'1.0'},'capabilities':{'experimentalApi':True}})
  self.send({'method':'initialized'})
  return self
 def __exit__(self,*a):self.s.close()
 def send(self,obj):
  p=json.dumps(obj,separators=(',',':')).encode();mask=os.urandom(4);n=len(p)
  h=b'\x81'+(bytes([128+n]) if n<126 else b'\xfe'+struct.pack('!H',n) if n<65536 else b'\xff'+struct.pack('!Q',n))
  self.s.sendall(h+mask+bytes(v^mask[i%4] for i,v in enumerate(p)))
 def take(self,n):
  b=b''
  while len(b)<n:
   part=self.s.recv(n-len(b))
   if not part:raise RuntimeError('WebSocket closed')
   b+=part
  return b
 def recv(self):
  a,b=self.take(2);op=a&15;n=b&127
  if n==126:n=struct.unpack('!H',self.take(2))[0]
  elif n==127:n=struct.unpack('!Q',self.take(8))[0]
  m=self.take(4) if b&128 else None;p=self.take(n)
  if m:p=bytes(v^m[i%4] for i,v in enumerate(p))
  if op==8:raise RuntimeError('WebSocket close frame')
  if op==9:
   mask=os.urandom(4);self.s.sendall(bytes([138,128+len(p)])+mask+bytes(v^mask[i%4] for i,v in enumerate(p)));return None
  if op==1:return json.loads(p)
  return None
 def call(self,method,params):
  self.i+=1;i=self.i;self.send({'jsonrpc':'2.0','id':i,'method':method,'params':params})
  while True:
   r=self.recv()
   if r is not None and r.get('id')==i:
    if 'error' in r:raise RuntimeError(json.dumps(r['error'],ensure_ascii=False))
    return r.get('result',{})
