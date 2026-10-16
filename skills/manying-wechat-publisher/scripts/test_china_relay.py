import socket
import struct
import unittest
from unittest.mock import patch

import china_relay as relay


class RelayTests(unittest.TestCase):
    def test_only_wechat_tls_and_explicit_exit_probe_are_allowed(self):
        for host in ['mp.weixin.qq.com','mmbiz.qpic.cn','res.wx.qq.com','myip.ipip.net']:
            self.assertTrue(relay.allowed(host,443))
        for host,port in [('mp.weixin.qq.com.evil.example',443),('localhost',443),
                          ('127.0.0.1',443),('mp.weixin.qq.com',80),('auth.openai.com',443)]:
            self.assertFalse(relay.allowed(host,port))

    def test_dns_cannot_redirect_relay_to_a_private_address(self):
        with patch.object(socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]),patch.object(socket,'socket') as connect:
            with self.assertRaises(OSError):relay.public_connection('mp.weixin.qq.com',443)
            connect.assert_not_called()

    def test_windows_tun_fake_ip_is_kept_without_disabling_tun(self):
        with patch.object(relay.os,'name','nt'),patch.object(socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.1.180',443))]),patch.object(socket,'socket') as connect:
            self.assertIs(relay.public_connection('mp.weixin.qq.com',443),connect.return_value)
            connect.return_value.connect.assert_called_once_with(('198.18.1.180',443))

    def test_physical_dns_rejects_spoofed_ids_and_private_answers(self):
        question=b'\x01a\0'+struct.pack('!HH',1,1)
        header=struct.pack('!6H',72,0x8180,1,2,0,0)
        def record(ip):
            return b'\xc0\x0c'+struct.pack('!HHIH',1,1,60,4)+socket.inet_aton(ip)
        data=header+question+record('127.0.0.1')+record('8.8.8.8')
        self.assertEqual(relay.dns_answers(data,72),['8.8.8.8'])
        with self.assertRaises(OSError):relay.dns_answers(data,73)

    def test_physical_failure_never_falls_back_to_system_tun_dns(self):
        with patch.object(relay,'INTERFACE',20),patch.object(relay,'physical_addresses',side_effect=OSError),patch.object(socket,'getaddrinfo') as system:
            with self.assertRaises(OSError):relay.public_connection('mp.weixin.qq.com',443)
            system.assert_not_called()


if __name__=='__main__':unittest.main()
