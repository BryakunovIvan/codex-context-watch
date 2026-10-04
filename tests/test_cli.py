import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def write_session(path, text='hello', identity='chat-1'):
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {'type': 'session_meta', 'payload': {'id': identity, 'cwd': '/project'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': text}]}},
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
             'last_token_usage': {'input_tokens': 100, 'output_tokens': 10},
             'total_token_usage': {'input_tokens': 9000}, 'model_context_window': 1000}}},
    ]
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


class CliTests(unittest.TestCase):
    def run_cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, '-m', 'context_watch', *args],
                              cwd=ROOT, text=True, capture_output=True, timeout=10, **kwargs)

    def test_report_json_and_inspect_trace_the_actual_record(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'rollout.jsonl'
            write_session(path, 'unique content')
            result = self.run_cli('report', '--file', str(path), '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['usage']['input_tokens'], 100)
            self.assertEqual(report['entries'][0]['bytes'], 14)
            self.assertNotIn('text', report['entries'][0])
            detail = self.run_cli('inspect', '--file', str(path), '--item', report['entries'][0]['id'])
            self.assertEqual(detail.returncode, 0, detail.stderr)
            self.assertIn('unique content', detail.stdout)
            self.assertIn('line=2', detail.stdout)

    def test_session_discovery_supports_codex_home_and_selection_by_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sessions' / '2026' / '10' / '04' / 'rollout-test.jsonl'
            write_session(p)
            result = self.run_cli('sessions', '--home', d, '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]['id'], 'chat-1')
            report = self.run_cli('report', '--home', d, '--session', 'chat-1', '--json')
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertEqual(json.loads(report.stdout)['metadata']['id'], 'chat-1')

    def test_missing_session_fails_cleanly_without_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.run_cli('report', '--home', d)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Traceback', result.stderr)

    def test_terminal_control_characters_are_escaped_in_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            write_session(p, 'hello\x1b[2J\x07')
            result = self.run_cli('inspect', '--file', str(p), '--item', 'e000001')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('\x1b', result.stdout)
            self.assertNotIn('\x07', result.stdout)
            self.assertIn('\\x1b', result.stdout)

    def test_plain_watch_reads_appended_lines_and_stays_pinned(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sessions' / '2026' / '10' / '04' / 'rollout-first.jsonl'
            write_session(p, 'first', 'first-chat')
            process = subprocess.Popen([sys.executable, '-m', 'context_watch', 'watch',
                '--home', d, '--plain', '--json', '--interval', '0.05', '--iterations', '8'],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                first = json.loads(process.stdout.readline())
                self.assertEqual(first['metadata']['id'], 'first-chat')
                write_session(p.parent / 'rollout-other.jsonl', 'other', 'other-chat')
                with p.open('a') as f:
                    f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message',
                        'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'appended'}]}}) + '\n')
                rest, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, err)
                snapshots = [json.loads(line) for line in rest.splitlines()]
                self.assertTrue(any(s['visible_bytes'] == 13 for s in snapshots))
                self.assertTrue(all(s['metadata']['id'] == 'first-chat' for s in snapshots))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_watch_rejects_zero_interval(self):
        result = self.run_cli('watch', '--interval', '0')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', result.stderr)

    def test_standalone_launcher_works_from_another_directory(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            write_session(p)
            result = subprocess.run([sys.executable, str(ROOT / 'codex-context'),
                'watch', '--once', '--file', str(p), '--json'], cwd=d,
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['usage']['input_tokens'], 100)

    def test_ambiguous_session_prefix_and_unknown_item_are_clean_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sessions' / 'first.jsonl'
            write_session(p, identity='chat-1')
            write_session(p.parent / 'second.jsonl', identity='chat-2')
            result = self.run_cli('report', '--home', d, '--session', 'chat')
            self.assertEqual(result.returncode, 2)
            result = self.run_cli('inspect', '--file', str(p), '--item', 'missing')
            self.assertEqual(result.returncode, 2)

    def test_offline_archived_session_discovery_with_broken_optional_database(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'archived_sessions' / 'archived.jsonl'
            write_session(p, identity='archived-chat')
            (Path(d) / 'state_5.sqlite').write_text('invalid database')
            result = self.run_cli('sessions', '--home', d, '--archived', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]['id'], 'archived-chat')


if __name__ == '__main__':
    unittest.main()
