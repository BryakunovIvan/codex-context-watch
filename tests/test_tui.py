import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'posix', 'curses TUI requires a POSIX terminal')
class TerminalTests(unittest.TestCase):
    def test_file_view_keys_scrolling_search_and_small_terminal(self):
        import curses
        from types import SimpleNamespace
        from context_watch.reader import RolloutReader
        from context_watch.tui import run

        class Screen:
            def __init__(self):
                self.lines = {}
                self.frames = []
                self.dimensions = (18, 150)
                self.keys = iter(['6', curses.KEY_NPAGE, curses.KEY_END, '\n',
                                  curses.KEY_NPAGE, curses.KEY_HOME, 'q',
                                  '/', *'f00.py', '\n', 'c', curses.KEY_HOME,
                                  'm', curses.KEY_LEFT, curses.KEY_RIGHT, 'h', 'h',
                                  '/', *'f29.py', '\n', 'c', 'm', 'resize', 'q'])

            def getmaxyx(self):
                return self.dimensions

            def addnstr(self, y, x, text, length, attr):
                self.lines[y] = text[:length]

            def erase(self):
                self.lines = {}

            def refresh(self):
                self.frames.append('\n'.join(self.lines[y] for y in sorted(self.lines)))

            def get_wch(self):
                key = next(self.keys)
                if key == 'resize':
                    self.dimensions = (10, 60)
                    return curses.KEY_RESIZE
                return key

            def keypad(self, enabled):
                pass

            def timeout(self, value):
                pass

            def move(self, y, x):
                pass

            def clrtoeol(self):
                pass

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'rollout.jsonl'
            records = [{'type': 'session_meta', 'payload': {'cwd': '/project'}}]
            records.extend({'type': 'response_item', 'payload': {'type': 'function_call',
                'name': 'exec_command', 'call_id': str(i),
                'arguments': json.dumps({'cmd': f'cat f{i:02}.py'})}} for i in range(30))
            records.append({'type': 'response_item', 'payload': {'type': 'function_call',
                'name': 'exec_command', 'call_id': 'md', 'arguments': '{"cmd":"cat A.MD"}'}})
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            reader = RolloutReader(path)
            screen = Screen()
            args = SimpleNamespace(scope='active', filter='', files='all', interval=1, iterations=None)
            with patch('context_watch.tui.curses.curs_set'), patch('context_watch.tui.curses.has_colors', return_value=False):
                run(screen, reader, args, None)
            file_frames = [frame for frame in screen.frames if '6 Файлы | WATCH' in frame]
            self.assertTrue(any('/project/f05.py' in frame for frame in file_frames))
            self.assertTrue(any('/project/f29.py' in frame for frame in file_frames))
            self.assertTrue(any('поиск: f00.py | 1 строк' in frame for frame in file_frames))
            self.assertTrue(any('5 События | WATCH' in frame for frame in screen.frames))
            self.assertTrue(any('Файл (предположение): Чтение | /project/f29.py' in frame for frame in screen.frames))
            md_frames = [frame for frame in file_frames if 'только .md (m)' in frame]
            self.assertTrue(any('/project/A.MD' in frame and '1 строк' in frame for frame in md_frames))
            self.assertTrue(any('поиск: f29.py | 0 строк' in frame and 'Нет файлов' in frame for frame in md_frames))
            self.assertTrue(all('/project/f' not in frame for frame in md_frames))
            self.assertIn('все файлы (m)', file_frames[-1])
            self.assertIn('Увеличьте терминал', screen.frames[-1])

    def test_primary_watch_exposes_incomplete_data_and_diagnostic_details(self):
        import fcntl
        import pty
        import struct
        import termios
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'rollout.jsonl'
            records = [{'type': 'compacted', 'payload': {'message': 'partial'}},
                       {'type': 'future_schema', 'payload': {}},
                       {'type': 'response_item', 'payload': {'type': 'custom_tool_call',
                        'name': 'apply_patch', 'call_id': 'p',
                        'input': '*** Begin Patch\n*** Add File: created.py\n+fixture\n*** End Patch'}}]
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 150, 0, 0))
            env = dict(os.environ, TERM='xterm-256color')
            process = subprocess.Popen([sys.executable, '-m', 'context_watch', 'watch',
                '--file', str(path), '--files', 'md', '--interval', '0.05'], cwd=root,
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
                output.clear()
                os.write(master, b'6')
                until('только .md')
                until('Нет файлов')
                output.clear()
                os.write(master, b'm')
                until('все файлы')
                until('Создание')
                until('[предположение]')
                self.assertIn(b'\x1b[', output)
                output.clear()
                os.write(master, b'm')
                until('только .md')
                until('Нет файлов')
                output.clear()
                os.write(master, b'm')
                until('все файлы')
                until('created.py')
                output.clear()
                os.write(master, b'\n')
                until('Файл (предположение): Создание | created.py')
                os.write(master, b'q')
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
