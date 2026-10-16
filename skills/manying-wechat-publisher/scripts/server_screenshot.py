#!/usr/bin/env python3
"""User-authorized screenshot of the private server login desktop; no browser tool."""
import argparse
import os
from pathlib import Path
import subprocess

from china_relay import ssh_proxy_option

REMOTE = r'''
import socket,struct,zlib,sys,time
s=socket.create_connection(('127.0.0.1',5907),timeout=10);s.settimeout(15)
def take(n):
    data=b''
    while len(data)<n:
        piece=s.recv(n-len(data))
        if not piece:raise OSError('Private desktop closed')
        data+=piece
    return data
take(12);s.sendall(b'RFB 003.008\n')
count=take(1)[0];types=take(count)
if 1 not in types:raise RuntimeError('Unexpected private desktop authentication')
s.sendall(b'\x01')
if struct.unpack('!I',take(4))[0]:raise RuntimeError('Private desktop authentication failed')
s.sendall(b'\x01');header=take(24)
width,height=struct.unpack_from('!HH',header);take(struct.unpack_from('!I',header,20)[0])
if not(100<=width<=4096 and 100<=height<=4096):raise RuntimeError('Unexpected desktop dimensions')
pixel=struct.pack('!BBBBHHHBBB3x',32,24,0,1,255,255,255,0,8,16)
s.sendall(b'\x00\x00\x00\x00'+pixel);s.sendall(struct.pack('!BBHi',2,0,1,0))
def request():s.sendall(struct.pack('!BBHHHH',3,0,0,0,width,height))
request();frame=bytearray(width*height*3);deadline=time.monotonic()+15
while time.monotonic()<deadline:
    typ=take(1)[0]
    if typ==2:continue
    if typ==3:
        take(3);take(struct.unpack('!I',take(4))[0]);continue
    if typ!=0:raise RuntimeError('Unexpected desktop update')
    rectangles=struct.unpack('!H',take(3)[1:])[0]
    area=0
    for _ in range(rectangles):
        x,y,w,h,encoding=struct.unpack('!HHHHi',take(12))
        if encoding!=0 or x+w>width or y+h>height:raise RuntimeError('Unexpected pixel rectangle')
        data=take(w*h*4);area+=w*h
        for row in range(h):
            pixels=data[row*w*4:(row+1)*w*4];rgb=bytearray(w*3)
            rgb[0::3]=pixels[0::4];rgb[1::3]=pixels[1::4];rgb[2::3]=pixels[2::4]
            off=((y+row)*width+x)*3;frame[off:off+w*3]=rgb
    # A black Xvfb desktop can contain only a tiny white cursor; that is not a login image.
    if area>=width*height and sum(frame[0::3])>width*height*15:break
    time.sleep(.2);request()
else:raise RuntimeError('Desktop is black or incomplete; screenshot was not saved')
s.close()
def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
rows=b''.join(b'\x00'+frame[i*width*3:(i+1)*width*3] for i in range(height))
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')
sys.stdout.buffer.write(png)
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--interface',type=int,required=True)
    parser.add_argument('--authorized-login-image',action='store_true',required=True)
    args=parser.parse_args()
    target=Path(args.output)
    if not target.is_absolute() or not target.parent.is_dir():parser.error('Use the actual existing local destination')
    command=['ssh',*ssh_proxy_option(args.interface),'-T','-i',str(Path.home()/'.ssh/manying_deploy_20260929'),
             '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=12',
             'ubuntu@43.134.92.99','python3 -']
    result=subprocess.run(command,input=REMOTE.encode(),capture_output=True,timeout=45,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode or not result.stdout.startswith(b'\x89PNG\r\n\x1a\n'):
        raise RuntimeError('Login screenshot unavailable; no authentication content printed')
    target.write_bytes(result.stdout)
    print({'screenshot':str(target),'bytes':len(result.stdout),'must_visually_check':True})


if __name__=='__main__':main()
