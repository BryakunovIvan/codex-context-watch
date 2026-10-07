import unittest

from context_watch.analyzer import Analyzer
from test_core import message, record


class ViewTests(unittest.TestCase):
    def test_file_modes_filter_paths_and_search_without_changing_the_report(self):
        import copy
        from context_watch.display import render_report
        from context_watch.views import rows_for, related_entries
        a = Analyzer()
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'x', 'arguments': '{"cmd":"cat README.md Guide.MD code.py notes.md.bak folder.md/code.py"}'}))
        a.feed(record('response_item', {'type': 'function_call_output', 'call_id': 'x',
            'output': 'needle'}))
        report = a.report()
        original = copy.deepcopy(report)
        self.assertEqual(len(rows_for(report, 'files', file_mode='all')), 5)
        md_rows = rows_for(report, 'files', file_mode='md')
        self.assertEqual([row['file'] for row in md_rows], ['Guide.MD', 'README.md'])
        self.assertEqual(len(related_entries(report, md_rows[0])), 2)
        self.assertEqual(len(rows_for(report, 'files', 'needle', 'md')), 2)
        # A match on a hidden file path must not fall back to another file
        # from the same call's text.
        self.assertEqual(rows_for(report, 'files', 'code.py', 'md'), [])
        self.assertEqual(len(rows_for(report, 'tools', file_mode='md')), 1)
        text = render_report(report, top=1, file_mode='md').split('Файлы (только .md)', 1)[1]
        self.assertIn('Guide.MD', text)
        self.assertNotIn('README.md', text)
        self.assertNotIn('code.py', text)
        self.assertEqual(report, original)

    def test_md_mode_handles_empty_list_and_history(self):
        from context_watch.display import render_report
        from context_watch.views import rows_for
        a = Analyzer()
        a.feed(record('response_item', {'type': 'custom_tool_call', 'name': 'apply_patch',
            'input': '*** Begin Patch\n*** Add File: README.md\n+fixture\n*** End Patch'}))
        a.feed(record('compacted', {'message': 'summary'}))
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'arguments': '{"cmd":"cat code.py"}'}))
        self.assertEqual(rows_for(a.report(), 'files', file_mode='md'), [])
        self.assertEqual(len(rows_for(a.report('history'), 'files', file_mode='md')), 1)
        self.assertIn('Нет файлов для выбранного режима', render_report(a.report(), file_mode='md'))

    def test_file_view_searches_normalized_path_and_full_output_without_changing_totals(self):
        from context_watch.views import rows_for, related_entries
        from context_watch.display import render_report, entry_detail
        a = Analyzer()
        a.feed(record('session_meta', {'cwd': '/project'}))
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'x', 'arguments': '{"cmd":"cat code.py other.json"}'}))
        a.feed(record('response_item', {'type': 'function_call_output', 'call_id': 'x',
            'output': 'x' * 300 + 'needle'}))
        report = a.report()
        total = report['visible_bytes']
        rows = rows_for(report, 'files', '/PROJECT/code.py')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['file'], '/project/code.py')
        self.assertIn('Чтение', rows[0]['text'])
        self.assertEqual(len(related_entries(report, rows[0])), 2)
        self.assertEqual(len(rows_for(report, 'files', 'needle')), 2)
        self.assertEqual(report['visible_bytes'], total)
        text = render_report(report, query='/project/code.py')
        self.assertIn('/project/code.py', text)
        self.assertNotIn('/project/other.json', text)
        self.assertIn('Файл (предположение)', entry_detail(report['entries'][0], '/log'))

    def test_file_paths_with_terminal_controls_are_escaped(self):
        import json
        from context_watch.display import render_report
        a = Analyzer()
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'arguments': json.dumps({'cmd': 'cat "bad\x1bfile.py"'})}))
        text = render_report(a.report())
        self.assertNotIn('\x1b', text)
        self.assertIn('\\x1b', text)

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
