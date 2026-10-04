import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == 'posix', 'curses TUI requires a POSIX terminal')
class TerminalTests(unittest.TestCase):
    def test_primary_watch_exposes_incomplete_data_and_diagnostic_details(self):
        import fcntl
        import pty
        import struct
        import termios
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'rollout.jsonl'
            records = [{'type': 'compacted', 'payload': {'message': 'partial'}},
                       {'type': 'future_schema', 'payload': {}}]
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 150, 0, 0))
            env = dict(os.environ, TERM='xterm-256color')
            process = subprocess.Popen([sys.executable, '-m', 'context_watch', 'watch',
                '--file', str(path), '--interval', '0.05'], cwd=root,
                stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            output = bytearray()

            def until(needle):
                wanted = needle.encode('utf-8')
                deadline = time.monotonic() + 4
                while wanted not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                self.assertIn(wanted, output, output.decode('utf-8', 'replace')[-2000:])

            try:
                until('частичное восстановление')
                os.write(master, b'd')
                until('future_schema')
                os.write(master, b'qq')
                # Keep draining the PTY while curses restores the screen on exit;
                # waiting first can block the child on a full terminal output buffer.
                deadline = time.monotonic() + 4
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                process.wait(timeout=1)
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(b'Traceback', output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)


if __name__ == '__main__':
    unittest.main()
