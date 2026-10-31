"""Versioned local skill registry. Skills select prompts/tools, never permissions."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'skills'


class SkillRegistry:
    def __init__(self, root=ROOT):
        self.items = {}
        for path in sorted(Path(root).glob('*/skill.json')):
            meta = json.loads(path.read_text())
            name = meta['id']
            if name in self.items or path.parent.name != name:
                raise ValueError('Duplicate/mismatched skill id')
            text = (path.parent / 'instructions.md').read_text()
            if not meta['version'] or not meta['description'] or len(text) > 6000:
                raise ValueError('Invalid skill definition')
            if set(meta['tools']) - {'search_more', 'submit_result'}:
                raise ValueError('Skill requests unsupported tool')
            self.items[name] = {**meta, 'instructions': text,
                'sha256': hashlib.sha256(path.read_bytes() + text.encode()).hexdigest()}
        if not self.items:
            raise ValueError('No skills installed')

    def catalog(self):
        return [{k: v[k] for k in ('id', 'description')} for v in self.items.values()]

    def load(self, name):
        if name not in self.items:
            raise ValueError('Unknown skill')
        return self.items[name]

    def fingerprint(self):
        return {k: v['sha256'] for k, v in self.items.items()}
