import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace

from cwm_panel.app import load_config
from cwm_panel.system import Daemon, Demo, OpenBSD, SystemError, parse_interfaces, parse_scan

IFCONFIG = '''lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 32768
    inet 127.0.0.1 netmask 0xff000000
em0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    media: Ethernet autoselect
    status: active
    inet 10.0.0.2 netmask 0xffffff00 broadcast 10.0.0.255
iwm0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    ieee80211: nwid "my cafe" chan 6 bssid aa:bb:cc:dd:ee:ff 82% wpakey <not displayed>
    status: active
    inet 192.168.1.24 netmask 0xffffff00 broadcast 192.168.1.255
'''


class SystemTests(unittest.TestCase):
    def test_parses_wired_and_wifi_without_loopback(self):
        wired, wifi = parse_interfaces(IFCONFIG)
        self.assertEqual(wired.address, '10.0.0.2')
        self.assertFalse(wired.wireless)
        self.assertEqual(wifi.ssid, 'my cafe')
        self.assertTrue(wifi.connected)
        self.assertTrue(wifi.up)
        self.assertEqual(wifi.signal, '82%')

    def test_scan_handles_spaces_duplicates_and_hidden_ssids(self):
        output = '''    nwid "my cafe" chan 6 bssid aa:bb 80% wpa2
    nwid "my cafe" chan 11 bssid aa:bc 40% wpa2
    nwid guest chan 1 bssid aa:bd
    nwid "" chan 2 bssid aa:be
'''
        self.assertEqual(parse_scan(output), [('guest', 'open / unknown'), ('my cafe', 'secured')])

    def test_reads_unknown_battery_and_missing_controls(self):
        def runner(args, **kw):
            values = {('ifconfig', '-a'): IFCONFIG, ('apm', '-l'): '255',
                      ('apm', '-a'): '255', ('apm', '-m'): '65535',
                      ('sndioctl', '-n', 'output.level', 'output.mute'): '.42\n1'}
            return SimpleNamespace(returncode=0, stdout=values[tuple(args)])
        host = OpenBSD({}, runner)
        host.daemon = SimpleNamespace(request=lambda *a: '72.00%')
        state = host.read()
        self.assertIsNone(state.battery)
        self.assertIsNone(state.ac)
        self.assertIsNone(state.minutes)
        self.assertEqual(state.volume, 42)
        self.assertTrue(state.muted)
        self.assertEqual(state.brightness, 72)

    def test_privileged_calls_go_only_through_daemon(self):
        calls = []
        host = OpenBSD({}, lambda *a, **k: self.fail('must not spawn subprocess'))
        host.daemon = SimpleNamespace(request=lambda *a: calls.append(a) or '')
        host.connect('iwm0', 'cafe ; wifi', 'pa$$ word')
        host.brightness(0)
        host.wifi('iwm0', False)
        host.power('low')
        self.assertEqual(calls, [('connect', 'iwm0', 'cafe ; wifi', 'pa$$ word'),
                                 ('brightness', 1), ('wifi', 'iwm0', 'down'), ('power', 'low')])

    def test_subprocess_failures_do_not_reveal_arguments(self):
        host = OpenBSD({}, lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
        with self.assertRaises(SystemError) as error:
            host.run('sndioctl', 'secret-value')
        self.assertNotIn('secret-value', str(error.exception))

    def test_invalid_config_fails_early(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            for invalid in [{'poll_seconds': -1}, {'height': '34'}, {'socket': 'relative'},
                            {'audio_devices': [{}]}, []]:
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_demo_changes_are_in_memory(self):
        host = Demo({})
        host.volume(12)
        host.mute(True)
        host.connect('iwm0', 'lab', 'password')
        self.assertEqual(host.read().volume, 12)
        self.assertTrue(host.read().muted)
        self.assertEqual(host.read().networks[0].ssid, 'lab')


class ProtocolTests(unittest.TestCase):
    def exchange(self, response):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = str(Path(folder.name)/'control.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)
        captured = []
        def serve():
            with server.accept()[0] as peer:
                with peer.makefile('rb') as stream:
                    captured.append(stream.readline())
                peer.sendall(response)
        thread = threading.Thread(target=serve)
        thread.start()
        self.addCleanup(thread.join)
        return Daemon(path), captured, thread

    def test_hex_protocol_with_unicode_and_secret(self):
        client, captured, thread = self.exchange(b'OK\t' + 'connected'.encode().hex().encode() + b'\n')
        self.assertEqual(client.request('connect', 'iwm0', '작업실', 'pa$$ word'), 'connected')
        thread.join()
        self.assertNotIn(b'pa$$ word', captured[0])
        self.assertEqual(captured[0].decode().split('\t')[2], '작업실'.encode().hex())

    def test_daemon_error_is_visible(self):
        message = 'brightness must be 1..100'
        client, _, _ = self.exchange(b'ERR\t' + message.encode().hex().encode() + b'\n')
        with self.assertRaisesRegex(SystemError, 'brightness must'):
            client.request('brightness', 0)

    def test_malformed_response_is_rejected(self):
        client, _, _ = self.exchange(b'OK\tzz\n')
        with self.assertRaises(SystemError):
            client.request('ping')


if __name__ == '__main__':
    unittest.main()
