import unittest

from context_watch.analyzer import Analyzer
from test_core import message, record


class ViewTests(unittest.TestCase):
    def test_largest_view_ranks_by_bytes_and_searches_full_contents(self):
        from context_watch.views import rows_for
        a = Analyzer()
        a.feed(message('small'))
        a.feed(message('x' * 200 + 'needle'))
        rows = rows_for(a.report(), 'largest', 'needle')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['entry']['bytes'], 206)

    def test_skill_and_tool_views_open_related_records_instead_of_guessing_a_read(self):
        from context_watch.views import rows_for, related_entries
        a = Analyzer()
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'x', 'arguments': '{"cmd":"cat /tmp/skill/SKILL.md"}'}))
        a.feed(record('response_item', {'type': 'function_call_output', 'call_id': 'x', 'output': 'body'}))
        skill_row = rows_for(a.report(), 'skills')[0]
        self.assertEqual([e['category'] for e in related_entries(a.report(), skill_row)], ['tool_call', 'tool_output'])
        tool_row = rows_for(a.report(), 'tools')[0]
        self.assertEqual(len(related_entries(a.report(), tool_row)), 2)

    def test_timeline_includes_compaction_even_without_text(self):
        from context_watch.views import rows_for
        a = Analyzer()
        a.feed(message('old'))
        a.feed(record('compacted', {'replacement_history': []}))
        rows = rows_for(a.report('history'), 'timeline')
        self.assertTrue(any(r.get('event', {}).get('type') == 'compacted' for r in rows))

    def test_watch_diagnostics_make_incomplete_and_skipped_data_visible(self):
        from context_watch.display import diagnostic_lines
        a = Analyzer()
        a.feed(record('compacted', {'message': 'partial'}))
        a.feed(record('future_schema', {}))
        a.malformed_lines = 1
        text = '\n'.join(diagnostic_lines(a.report()))
        self.assertIn('восстановление частичное', text)
        self.assertIn('future_schema', text)
        self.assertIn('1', text)


if __name__ == '__main__':
    unittest.main()
