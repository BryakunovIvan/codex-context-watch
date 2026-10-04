"""Analyze observable rollout text, without pretending it is the model's prompt."""
from collections import Counter
import math
from pathlib import PurePosixPath

from .attribution import analyze_call
MEDIA_TYPES = {'image', 'input_image', 'image_url', 'audio', 'input_audio',
               'output_audio', 'video', 'file', 'input_file'}


def visible_text(value):
    """Extract text blocks only. Never count base64 or encrypted blobs as text."""
    if isinstance(value, str):
        return value, 0
    if isinstance(value, list):
        parts, media = [], 0
        for block in value:
            text, count = visible_text(block)
            if text:
                parts.append(text)
            media += count
        return '\n'.join(parts), media
    if isinstance(value, dict):
        if isinstance(value.get('type'), str) and value['type'] in MEDIA_TYPES:
            return '', 1
        if isinstance(value.get('text'), str):
            return value['text'], 0
        if 'content' in value:
            return visible_text(value['content'])
        if 'output' in value:
            return visible_text(value['output'])
    return '', 0


def weight(text):
    return math.ceil(len(text.encode('utf-8', errors='replace')) / 4)


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


class Analyzer:
    def __init__(self):
        self.entries = []
        self.active = []
        self.calls = {}
        self.usage = {}
        self.metadata = {}
        self.model = None
        self.window = None
        self.turn_id = None
        self.compactions = 0
        self.unknown = Counter()
        self.limitations = set()
        self.malformed_lines = 0
        self.invalid_records = 0
        self.record_count = 0
        self.timeline = []
        self.serial = 0
        self.epoch = 0
        self.usage_epoch = None

    def _entry(self, text, category, timestamp, line, **extra):
        self.serial += 1
        entry = {
            'id': f'e{self.serial:06d}', 'line': line, 'timestamp': timestamp,
            'turn_id': self.turn_id, 'epoch': self.epoch, 'category': category,
            'role': '', 'label': category, 'tool': '', 'call_id': '',
            'nested_tools': [], 'skill_paths': [], 'attribution': 'none',
            'read_paths': [],
            'text': text, 'bytes': len(text.encode('utf-8', errors='replace')),
            'characters': len(text), 'estimated_tokens': weight(text),
            'media_blocks': 0, 'replacement': False,
        }
        entry.update(extra)
        self.entries.append(entry)
        self.active.append(entry)
        if entry['media_blocks']:
            self.limitations.add('multimodal_unmeasured')
        return entry

    def _item(self, item, timestamp, line, replacement=False):
        if not isinstance(item, dict):
            self.unknown['response_item:invalid'] += 1
            return
        kind = item.get('type', 'unknown')
        if not isinstance(kind, str):
            self.invalid_records += 1
            return
        extra = {'replacement': replacement}
        if kind == 'message':
            role = item.get('role', 'unknown')
            if not isinstance(role, str):
                self.invalid_records += 1
                return
            text, media = visible_text(item.get('content', []))
            category = 'instructions' if role in ('system', 'developer') else role
            self._entry(text, category, timestamp, line, role=role,
                        label=f'{role}: {text[:100]}', media_blocks=media, **extra)
        elif kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
            name = item.get('name', 'local_shell' if kind == 'local_shell_call' else 'unknown')
            call_id = item.get('call_id', item.get('id', ''))
            if not isinstance(name, str) or not isinstance(call_id, str):
                self.invalid_records += 1
                return
            text = item.get('arguments', item.get('input', ''))
            if not isinstance(text, str):
                import json
                text = json.dumps(text, ensure_ascii=False)
            nested, paths, attribution, resources = analyze_call(name, text)
            # A path in arguments is evidence of a reference, not proof the read succeeded.
            entry = self._entry(text, 'tool_call', timestamp, line,
                               label=f'{name}: {text[:100]}', tool=name, call_id=call_id,
                               skill_paths=paths, nested_tools=nested,
                               attribution=attribution, read_paths=resources, **extra)
            if call_id:
                self.calls[call_id] = entry
        elif kind in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
            call_id = item.get('call_id', '')
            if not isinstance(call_id, str):
                self.invalid_records += 1
                return
            text, media = visible_text(item.get('output', ''))
            call = self.calls.get(call_id, {})
            name = call.get('tool', 'unmatched')
            self._entry(text, 'tool_output', timestamp, line,
                        label=f'{name} → {text[:100]}', tool=name, call_id=call_id,
                        skill_paths=call.get('skill_paths', []),
                        nested_tools=call.get('nested_tools', []),
                        read_paths=call.get('read_paths', []),
                        attribution=call.get('attribution', 'unmatched'),
                        media_blocks=media, **extra)
        elif kind == 'reasoning':
            text, media = visible_text(item.get('summary', []))
            if item.get('encrypted_content'):
                self.limitations.add('opaque_reasoning')
            if text:
                self._entry(text, 'reasoning', timestamp, line,
                            label=f'reasoning summary: {text[:100]}', media_blocks=media, **extra)
        elif kind == 'compaction':
            self.limitations.add('opaque_compaction')
        else:
            self.unknown[f'response_item:{kind}'] += 1

    def _usage(self, last, total, timestamp, source):
        if not isinstance(last, dict) or not last:
            return
        total = total if isinstance(total, dict) else {}
        previous_total = {k: v for k, v in self.usage.items() if k.startswith('cumulative_')}
        self.usage = {k: number(v) for k, v in last.items() if number(v) is not None}
        self.usage.update(previous_total)
        self.usage.update({'cumulative_' + k: number(v) for k, v in total.items()
                           if number(v) is not None})
        self.usage.update(timestamp=timestamp, source=source,
                          context_window_at_measurement=self.window,
                          visible_tokens_at_measurement=sum(e['estimated_tokens'] for e in self.active),
                          entry_serial_at_measurement=self.serial)
        self.usage_epoch = self.epoch

    def feed(self, record, line=0):
        if not isinstance(record, dict):
            self.malformed_lines += 1
            return
        self.record_count += 1
        kind = record.get('type', 'unknown')
        if not isinstance(kind, str):
            self.invalid_records += 1
            return
        payload = record.get('payload', {})
        if not isinstance(payload, dict):
            self.invalid_records += 1
            return
        timestamp = record.get('timestamp', '')
        if kind == 'session_meta':
            self.metadata = {k: payload[k] for k in ('id', 'session_id', 'cwd', 'cli_version',
                             'originator', 'source', 'history_mode') if k in payload}
            base = payload.get('base_instructions', {})
            text = base.get('text', '') if isinstance(base, dict) else base
            if isinstance(text, str) and text:
                self._entry(text, 'base_instructions', timestamp, line,
                            label='Base instructions (session metadata)')
                self.limitations.add('base_instructions_from_metadata')
        elif kind == 'response_item':
            self._item(payload, timestamp, line)
        elif kind == 'turn_context':
            self.model = payload.get('model', self.model)
            self.turn_id = payload.get('turn_id', self.turn_id)
        elif kind == 'token_usage_record':
            self._usage(payload.get('usage', {}), payload.get('thread_token_usage', {}),
                        timestamp, kind)
        elif kind == 'event_msg':
            subtype = payload.get('type')
            if subtype == 'token_count':
                info = payload.get('info') or {}
                if isinstance(info, dict):
                    self.window = number(info.get('model_context_window')) or self.window
                    self._usage(info.get('last_token_usage', {}), info.get('total_token_usage', {}),
                                timestamp, subtype)
            elif subtype == 'task_started':
                self.turn_id = payload.get('turn_id', self.turn_id)
                self.window = number(payload.get('model_context_window')) or self.window
            elif subtype in ('context_compacted', 'task_complete', 'task_completed', 'task_started',
                             'turn_aborted', 'error'):
                self.timeline.append({'type': subtype, 'timestamp': timestamp, 'line': line})
            # event_msg user/assistant/item_completed are projections of response_item.
        elif kind == 'compacted':
            self.compactions += 1
            self.epoch += 1
            self.timeline.append({'type': 'compacted', 'timestamp': timestamp, 'line': line})
            self.active = [e for e in self.active if e['category'] == 'base_instructions']
            self.calls = {}
            replacement = payload.get('replacement_history')
            if isinstance(replacement, list):
                for item in replacement:
                    self._item(item, timestamp, line, replacement=True)
            else:
                self.limitations.add('incomplete_compaction')
                text = payload.get('message', '')
                if isinstance(text, str) and text:
                    self._entry(text, 'compaction_summary', timestamp, line,
                                label='Compaction summary (partial reconstruction)', replacement=True)
        elif kind in ('world_state', 'session_state', 'context_window'):
            pass
        else:
            self.unknown[str(kind)] += 1

    def report(self, scope='active'):
        if scope not in ('active', 'history'):
            raise ValueError('scope must be active or history')
        # Replacement snapshots belong to active history, not historical cost/growth.
        entries = self.active if scope == 'active' else [e for e in self.entries if not e['replacement']]
        categories, tools, nested_tools, skills = {}, {}, {}, {}
        for e in entries:
            category = categories.setdefault(e['category'], {'name': e['category'], 'count': 0,
                'bytes': 0, 'estimated_tokens': 0})
            category['count'] += 1
            category['bytes'] += e['bytes']
            category['estimated_tokens'] += e['estimated_tokens']
            if e['tool']:
                tool = tools.setdefault(e['tool'], {'name': e['tool'], 'calls': 0,
                    'output_count': 0, 'argument_bytes': 0, 'output_bytes': 0,
                    'estimated_tokens': 0})
                tool['calls'] += e['category'] == 'tool_call'
                tool['output_count'] += e['category'] == 'tool_output'
                tool['argument_bytes' if e['category'] == 'tool_call' else 'output_bytes'] += e['bytes']
                tool['estimated_tokens'] += e['estimated_tokens']
            for name in e['nested_tools']:
                nested = nested_tools.setdefault(name, {'name': name, 'calls': 0,
                    'estimated_tokens': 0, 'shared': True})
                nested['calls'] += e['category'] == 'tool_call'
                nested['estimated_tokens'] += e['estimated_tokens']
            for path in e['skill_paths']:
                skill = skills.setdefault(path, {'path': path, 'name': PurePosixPath(path).parent.name or path,
                    'calls': 0, 'argument_bytes': 0, 'output_bytes': 0,
                    'estimated_tokens': 0, 'shared': False, 'entry_ids': [], 'resources': []})
                skill['calls'] += e['category'] == 'tool_call'
                skill['argument_bytes' if e['category'] == 'tool_call' else 'output_bytes'] += e['bytes']
                skill['estimated_tokens'] += e['estimated_tokens']
                skill['shared'] |= e['attribution'] == 'shared/inferred'
                skill['entry_ids'].append(e['id'])
                skill['resources'] = list(dict.fromkeys(skill['resources'] + [p for p in e['read_paths']
                    if p == path or p.startswith(str(PurePosixPath(path).parent) + '/')]))
        usage = dict(self.usage)
        if usage:
            usage['new_visible_tokens'] = sum(e['estimated_tokens'] for e in self.active
                if e['id'] and int(e['id'][1:]) > usage['entry_serial_at_measurement'])
            usage['before_compaction'] = self.usage_epoch != self.epoch
            input_tokens = usage.get('input_tokens')
            measured_window = usage.get('context_window_at_measurement')
            usage['input_percent'] = round(input_tokens / measured_window * 100, 2) if measured_window and input_tokens is not None else None
        return {
            'schema_version': 1, 'scope': scope, 'metadata': dict(self.metadata),
            'model': self.model, 'context_window': self.window, 'usage': usage,
            'estimator': 'ceil(UTF-8 bytes / 4); approximate text weight, not model tokenization',
            'entries': list(entries), 'estimated_visible_tokens': sum(e['estimated_tokens'] for e in entries),
            'visible_bytes': sum(e['bytes'] for e in entries),
            'categories': sorted(categories.values(), key=lambda x: x['bytes'], reverse=True),
            'tools': sorted(tools.values(), key=lambda x: x['estimated_tokens'], reverse=True),
            'nested_tools': sorted(nested_tools.values(), key=lambda x: x['estimated_tokens'], reverse=True),
            'skills': sorted(skills.values(), key=lambda x: x['estimated_tokens'], reverse=True),
            'compactions': self.compactions, 'timeline': list(self.timeline),
            'record_count': self.record_count, 'malformed_lines': self.malformed_lines,
            'invalid_records': self.invalid_records,
            'unknown_records': dict(self.unknown), 'limitations': sorted(self.limitations),
        }
