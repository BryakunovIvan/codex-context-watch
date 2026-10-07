import json
import unittest

from context_watch.analyzer import Analyzer
from context_watch.attribution import analyze_files
from test_core import message, record


class FileTests(unittest.TestCase):
    def test_multiline_commands_and_comments_do_not_create_fake_paths(self):
        accesses = analyze_files('exec_command', json.dumps({'cmd':
            'cat first.py # cat fake.py; cat another-fake.py\ncat second.py\nhead -n 2 third.py'}))
        self.assertEqual([a['path'] for a in accesses], ['first.py', 'second.py', 'third.py'])
        accesses = analyze_files('exec_command', json.dumps({'cmd': 'cat 2 ""'}))
        self.assertEqual([a['path'] for a in accesses], ['2'])

    def test_reads_all_extensions_and_quoted_paths_with_workdir(self):
        accesses = analyze_files('functions.exec_command', json.dumps({
            'cmd': 'cat "My File.py" README; head -n 20 config.json; tail -c 8 data.csv',
            'workdir': 'src'}), '/project')
        self.assertEqual([a['path'] for a in accesses], [
            '/project/src/My File.py', '/project/src/README',
            '/project/src/config.json', '/project/src/data.csv'])
        self.assertTrue(all(a['operation'] == 'read' for a in accesses))

    def test_sed_and_rg_do_not_treat_patterns_options_and_outputs_as_files(self):
        accesses = analyze_files('exec_command', json.dumps({'cmd':
            "sed -n '1,80p' code.py; rg -n -g '*.py' 'needle' code.py other.py > results.txt 2>&1"}))
        self.assertEqual([a['path'] for a in accesses], ['code.py', 'other.py'])
        self.assertEqual(analyze_files('exec_command', '{"cmd":"sed --in-place s/a/b/ code.py"}'), [])

    def test_dynamic_commands_and_embedded_data_are_not_file_reads(self):
        commands = ['echo "cat fake.py"', "cat <<'EOF'\ncat fake.py\nEOF",
                    'python3 -c "print(\'cat fake.py\')"', 'cat $TARGET *.py ~/file.py',
                    'cd /other && cat relative.py', 'cat "unterminated']
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(analyze_files('exec_command', json.dumps({'cmd': command})), [])
        self.assertEqual(analyze_files('functions.exec',
            '// tools.exec_command({cmd:"cat fake.py"})\ntext("tools.exec_command({cmd: foo})")'), [])
        for arguments in ('{cmd: "cat wrong.py" + suffix}',
                          '{cmd: "cat wrong.py", workdir: getDirectory()}',
                          '{cmd: "cat wrong.py", workdir: "/wrong" + suffix}'):
            with self.subTest(arguments=arguments):
                self.assertEqual(analyze_files('functions.exec',
                    'tools.exec_command(' + arguments + ')', '/project'), [])

    def test_patch_paths_are_literal_even_when_they_contain_shell_metacharacters(self):
        patch = '*** Begin Patch\n*** Add File: app/[slug]/$page.py\n+fixture\n*** End Patch'
        self.assertEqual(analyze_files('apply_patch', patch, '/project')[0]['path'],
                         '/project/app/[slug]/$page.py')

    def test_patch_operations_include_move_and_ignore_code_body(self):
        patch = ('*** Begin Patch\n*** Add File: new file.py\n+cat fake.py\n'
                 '+*** Delete File: fake.txt\n*** Update File: old.py\n'
                 '*** Move to: moved.py\n@@\n-a\n+b\n*** Update File: code.py\n'
                 '@@\n-a\n+b\n*** Delete File: gone.py\n*** End Patch\n')
        accesses = analyze_files('functions.apply_patch', patch, '/project')
        self.assertEqual([(a['path'], a['operation']) for a in accesses], [
            ('/project/new file.py', 'create'), ('/project/old.py', 'move_from'),
            ('/project/moved.py', 'move_to'), ('/project/code.py', 'modify'),
            ('/project/gone.py', 'delete')])

    def test_nested_reads_and_literal_patches_are_linked(self):
        patch = '*** Begin Patch\n*** Update File: code.py\n@@\n-old\n+new\n*** End Patch'
        for literal in (json.dumps(patch), '`' + patch + '`'):
            with self.subTest(literal=literal[:20]):
                source = ('text(await tools.exec_command({cmd: "cat code.py", workdir: "/project"}));'
                          'text(await tools.apply_patch(' + literal + '));')
                accesses = analyze_files('functions.exec', source, '/project')
                self.assertEqual([(a['path'], a['operation']) for a in accesses], [
                    ('/project/code.py', 'read'), ('/project/code.py', 'modify')])
        self.assertEqual(analyze_files('functions.exec',
            'tools.apply_patch(`*** Begin Patch\n${patch}\n*** End Patch`)'), [])
        self.assertEqual(analyze_files('functions.exec', 'tools.apply_patch(patchVariable)'), [])

    def test_json_patch_argument_and_single_quoted_shell_command(self):
        patch = '*** Begin Patch\n*** Delete File: code.py\n*** End Patch'
        self.assertEqual(analyze_files('apply_patch', json.dumps({'patch': patch}))[0]['operation'], 'delete')
        self.assertEqual(analyze_files('functions.exec',
            "await tools.exec_command({cmd: 'cat code.py'})")[0]['path'], 'code.py')

    def test_call_and_failed_output_are_one_inferred_file_reference(self):
        a = Analyzer()
        a.feed(record('session_meta', {'cwd': '/project'}))
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'call_id': 'c', 'arguments': '{"cmd":"cat code.py code.py"}'}), line=2)
        a.feed(record('response_item', {'type': 'function_call_output', 'call_id': 'c',
            'output': 'No such file or directory'}), line=3)
        a.feed(message('Mentioning fake.py does not touch it'))
        report = a.report()
        self.assertEqual(len(report['files']), 1)
        file = report['files'][0]
        self.assertEqual(file['path'], '/project/code.py')
        self.assertEqual(file['calls'], 1)
        self.assertEqual(file['entry_ids'], ['e000001', 'e000002'])
        self.assertEqual(file['attribution'], 'inferred')
        self.assertEqual(report['entries'][0]['file_accesses'], report['entries'][1]['file_accesses'])

    def test_current_turn_cwd_and_normalized_paths_group_multiple_operations(self):
        a = Analyzer()
        a.feed(record('session_meta', {'cwd': '/old'}))
        a.feed(record('turn_context', {'cwd': '/project'}))
        a.feed(record('response_item', {'type': 'function_call', 'name': 'exec_command',
            'arguments': '{"cmd":"cat ./code.py"}'}))
        a.feed(record('response_item', {'type': 'custom_tool_call', 'name': 'apply_patch',
            'input': '*** Begin Patch\n*** Update File: /project/code.py\n@@\n-a\n+b\n*** End Patch'}))
        file = a.report()['files'][0]
        self.assertEqual(file['path'], '/project/code.py')
        self.assertEqual(file['operations'], ['read', 'modify'])
        self.assertEqual(file['calls'], 2)

    def test_compaction_keeps_file_scopes_separate_without_double_counting(self):
        a = Analyzer()
        item = {'type': 'function_call', 'name': 'exec_command', 'call_id': 'c',
                'arguments': '{"cmd":"cat old.py"}'}
        a.feed(record('response_item', item))
        a.feed(record('compacted', {'replacement_history': [item]}))
        self.assertEqual(a.report()['files'][0]['calls'], 1)
        self.assertEqual(a.report('history')['files'][0]['calls'], 1)
        a.feed(record('compacted', {'message': 'summary'}))
        self.assertEqual(a.report()['files'], [])
        self.assertEqual(a.report('history')['files'][0]['calls'], 1)


if __name__ == '__main__':
    unittest.main()
