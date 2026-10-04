import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from context_watch.analyzer import Analyzer


def record(kind, payload):
    return {'type': kind, 'timestamp': '2026-10-04T08:00:00Z', 'payload': payload}


def message(text, role='user'):
    return record('response_item', {'type': 'message', 'role': role,
                                  'content': [{'type': 'input_text', 'text': text}]})


class AnalyzerTests(unittest.TestCase):
    def test_counts_canonical_items_without_counting_event_mirrors(self):
        a = Analyzer()
        a.feed(message('abcdefgh'))
        a.feed(record('event_msg', {'type': 'item_completed', 'item':
                                   {'type': 'userMessage', 'text': 'abcdefgh'}}))
        self.assertEqual(len(a.report()['entries']), 1)
        self.assertEqual(a.report()['entries'][0]['bytes'], 8)
        self.assertEqual(a.report()['entries'][0]['estimated_tokens'], 2)

    def test_latest_request_is_not_cumulative_usage(self):
        a = Analyzer()
        a.feed(record('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 100, 'output_tokens': 20,
                                'cached_input_tokens': 80},
            'total_token_usage': {'input_tokens': 9000, 'output_tokens': 500},
            'model_context_window': 1000}}))
        self.assertEqual(a.report()['usage']['input_tokens'], 100)
        self.assertEqual(a.report()['usage']['cumulative_input_tokens'], 9000)
        self.assertEqual(a.report()['usage']['input_percent'], 10)

    def test_new_usage_schema_does_not_duplicate_legacy_measurement(self):
        a = Analyzer()
        a.feed(record('token_usage_record', {'usage': {'input_tokens': 200,
                'output_tokens': 30}, 'thread_token_usage': {'input_tokens': 5000}}))
        self.assertEqual(a.report()['usage']['input_tokens'], 200)
        self.assertEqual(a.report()['usage']['cumulative_input_tokens'], 5000)

    def test_structured_tool_output_is_linked_to_skill_read(self):
        a = Analyzer()
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'c1', 'arguments': json.dumps({'cmd': 'cat /tmp/my-skill/SKILL.md'})}))
        a.feed(record('response_item', {'type': 'function_call_output', 'call_id': 'c1',
            'output': [{'type': 'text', 'text': 'abcdefgh'},
                       {'type': 'image', 'data': 'x' * 10000}]}))
        report = a.report()
        output = next(e for e in report['entries'] if e['category'] == 'tool_output')
        self.assertEqual(output['text'], 'abcdefgh')
        self.assertEqual(output['tool'], 'exec_command')
        self.assertEqual(output['skill_paths'], ['/tmp/my-skill/SKILL.md'])
        self.assertEqual(report['skills'][0]['output_bytes'], 8)
        self.assertEqual(output['media_blocks'], 1)

    def test_nested_exec_tools_and_shared_skill_outputs_are_inferred(self):
        a = Analyzer()
        a.feed(record('response_item', {'type': 'custom_tool_call', 'name': 'functions.exec',
            'call_id': 'x', 'input': 'text(await tools.exec_command({cmd:"cat /a/SKILL.md /b/SKILL.md"})); text(await tools.web__run({}));'}))
        a.feed(record('response_item', {'type': 'custom_tool_call_output', 'call_id': 'x',
            'output': 'abcdefgh'}))
        output = a.report()['entries'][-1]
        self.assertEqual(output['nested_tools'], ['exec_command', 'web__run'])
        self.assertEqual(output['attribution'], 'shared/inferred')
        self.assertEqual(len(a.report()['skills']), 2)
        self.assertTrue(all(s['shared'] for s in a.report()['skills']))

    def test_compaction_replaces_active_history_and_preserves_historical_ranking(self):
        a = Analyzer()
        a.feed(message('old giant output'))
        a.feed(record('compacted', {'message': 'summary', 'replacement_history': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'kept'}]},
            {'type': 'compaction', 'encrypted_content': 'x' * 1000}]}))
        self.assertEqual([e['text'] for e in a.report()['entries']], ['kept'])
        self.assertEqual(a.report()['compactions'], 1)
        self.assertIn('old giant output', [e['text'] for e in a.report('history')['entries']])
        self.assertIn('opaque_compaction', a.report()['limitations'])

    def test_compaction_without_replacement_does_not_pretend_old_content_is_active(self):
        a = Analyzer()
        a.feed(message('old'))
        a.feed(record('compacted', {'message': 'summary'}))
        self.assertEqual([e['text'] for e in a.report()['entries']], ['summary'])
        self.assertIn('incomplete_compaction', a.report()['limitations'])

    def test_encrypted_reasoning_is_not_estimated_as_visible_text(self):
        a = Analyzer()
        a.feed(record('response_item', {'type': 'reasoning', 'encrypted_content': 'x' * 10000,
                'summary': [{'type': 'summary_text', 'text': 'thinking'}]}))
        self.assertEqual(a.report()['entries'][0]['bytes'], 8)
        self.assertIn('opaque_reasoning', a.report()['limitations'])

    def test_unrecognized_schema_is_visible_without_guessing_its_token_size(self):
        a = Analyzer()
        a.feed(record('future_schema', {'big_blob': 'x' * 1000}))
        self.assertEqual(a.report()['unknown_records'], {'future_schema': 1})
        self.assertEqual(a.report()['estimated_visible_tokens'], 0)

    def test_usage_checkpoint_distinguishes_new_items_from_last_measurement(self):
        a = Analyzer()
        a.feed(message('abcd'))
        a.feed(record('token_usage_record', {'usage': {'input_tokens': 100}}))
        a.feed(message('12345678', 'assistant'))
        self.assertEqual(a.report()['usage']['visible_tokens_at_measurement'], 1)
        self.assertEqual(a.report()['usage']['new_visible_tokens'], 2)

    def test_skill_paths_inside_code_being_written_are_not_skill_reads(self):
        a = Analyzer()
        script = 'text(await tools.apply_patch("*** Add File: /a/SKILL.md\\n+cat /b/SKILL.md\\n+tools.fake_tool({})"));'
        a.feed(record('response_item', {'type': 'custom_tool_call', 'name': 'functions.exec',
            'call_id': 'w', 'input': script}))
        a.feed(record('response_item', {'type': 'custom_tool_call_output', 'call_id': 'w', 'output': 'done'}))
        self.assertEqual(a.report()['skills'], [])
        self.assertEqual(a.report()['entries'][0]['nested_tools'], ['apply_patch'])

    def test_mixed_exec_output_is_shared_even_if_only_one_skill_is_read(self):
        a = Analyzer()
        a.feed(record('response_item', {'type': 'custom_tool_call', 'name': 'functions.exec',
            'call_id': 'x', 'input': 'text(await tools.exec_command({cmd:"cat /a/SKILL.md"})); text(await tools.web__run({}));'}))
        a.feed(record('response_item', {'type': 'custom_tool_call_output', 'call_id': 'x', 'output': 'mixed'}))
        self.assertTrue(a.report()['skills'][0]['shared'])

    def test_reading_plain_skill_filename_is_recognized(self):
        a = Analyzer()
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'x', 'arguments': '{"cmd":"cat SKILL.md"}'}))
        self.assertEqual(a.report()['skills'][0]['path'], 'SKILL.md')

    def test_reference_file_read_is_linked_to_its_skill_without_claiming_skill_body_read(self):
        a = Analyzer()
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'x', 'arguments': '{"cmd":"cat /tmp/skills/example/references/guide.md"}'}))
        skill = a.report()['skills'][0]
        self.assertEqual(skill['path'], '/tmp/skills/example/SKILL.md')
        self.assertEqual(skill['resources'], ['/tmp/skills/example/references/guide.md'])

    def test_negative_usage_does_not_produce_negative_context_percent(self):
        a = Analyzer()
        a.feed(record('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': -100}, 'model_context_window': 1000}}))
        self.assertIsNone(a.report()['usage'].get('input_percent'))

    def test_window_at_measurement_does_not_change_with_the_next_turn(self):
        a = Analyzer()
        a.feed(record('event_msg', {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 100}, 'model_context_window': 1000}}))
        a.feed(record('event_msg', {'type': 'task_started', 'model_context_window': 2000}))
        self.assertEqual(a.report()['usage']['input_percent'], 10)
        self.assertEqual(a.report()['usage']['context_window_at_measurement'], 1000)
        self.assertEqual(a.report()['context_window'], 2000)

    def test_long_nonpath_text_in_read_command_does_not_stall_the_scanner(self):
        code = "from context_watch.attribution import analyze_call; import json; result=analyze_call('exec_command',json.dumps({'cmd':'cat '+ 'a'*100000})); assert result[1] == []"
        try:
            result = subprocess.run([sys.executable, '-c', code], timeout=5,
                                    capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            self.fail('Path detection stalled on a long command without a Markdown path')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_skill_path_with_spaces_is_not_split_into_fake_skill_candidates(self):
        a = Analyzer()
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'x', 'arguments': json.dumps({'cmd': 'cat "/tmp/My Skill/SKILL.md"'})}))
        self.assertEqual([s['path'] for s in a.report()['skills']], ['/tmp/My Skill/SKILL.md'])


class ReaderTests(unittest.TestCase):
    def test_damaged_schema_records_do_not_hide_later_valid_messages(self):
        from context_watch.reader import RolloutReader
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            records = [record('response_item', {'type': 'function_call', 'name': None, 'arguments': '{}'}),
                record('response_item', {'type': 'message', 'role': ['user'], 'content': []}),
                {'type': ['bad'], 'payload': {}}, message('valid')]
            p.write_text(''.join(json.dumps(r) + '\n' for r in records))
            reader = RolloutReader(p)
            reader.poll()
            report = reader.analyzer.report()
            self.assertEqual([e['text'] for e in report['entries']], ['valid'])
            self.assertEqual(report['invalid_records'], 3)

    def test_rewrite_of_earlier_record_followed_by_append_is_replayed(self):
        from context_watch.reader import RolloutReader
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            first = json.dumps(message('old')) + '\n' + json.dumps(message('tail' * 100)) + '\n'
            p.write_text(first)
            reader = RolloutReader(p)
            reader.poll()
            p.write_text(first.replace('old', 'new') + json.dumps(message('appended')) + '\n')
            reader.poll()
            self.assertEqual([e['text'] for e in reader.analyzer.report()['entries']], ['new', 'tail' * 100, 'appended'])
            self.assertEqual(reader.resets, 1)

    def test_same_size_rewrite_and_file_replacement_are_replayed(self):
        from context_watch.reader import RolloutReader
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            first = json.dumps(message('old')) + '\n' + json.dumps(message('tail' * 100)) + '\n'
            p.write_text(first)
            reader = RolloutReader(p)
            reader.poll()
            p.write_text(first.replace('old', 'new'))
            reader.poll()
            self.assertEqual(reader.analyzer.report()['entries'][0]['text'], 'new')
            other = Path(d) / 'other'
            other.write_text(json.dumps(message('replaced')) + '\n')
            other.replace(p)
            reader.poll()
            self.assertEqual([e['text'] for e in reader.analyzer.report()['entries']], ['replaced'])

    def test_partial_utf8_line_is_not_lost_or_double_counted(self):
        from context_watch.reader import RolloutReader
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            raw = json.dumps(message('Привет'), ensure_ascii=False).encode() + b'\n'
            split = raw.index('П'.encode()) + 1
            p.write_bytes(raw[:split])
            reader = RolloutReader(p)
            reader.poll()
            self.assertEqual(len(reader.analyzer.report()['entries']), 0)
            with p.open('ab') as f:
                f.write(raw[split:])
            self.assertTrue(reader.poll())
            self.assertEqual(reader.analyzer.report()['entries'][0]['text'], 'Привет')
            self.assertFalse(reader.poll())

    def test_malformed_lines_do_not_hide_valid_records_and_truncation_resets(self):
        from context_watch.reader import RolloutReader
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'session.jsonl'
            p.write_text('broken\n' + json.dumps(message('first long message')) + '\n')
            reader = RolloutReader(p)
            reader.poll()
            self.assertEqual(reader.analyzer.report()['malformed_lines'], 1)
            p.write_text(json.dumps(message('new')) + '\n')
            reader.poll()
            self.assertEqual([e['text'] for e in reader.analyzer.report()['entries']], ['new'])


if __name__ == '__main__':
    unittest.main()
