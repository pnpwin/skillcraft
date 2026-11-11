"""Shared contracts, YAML handling and immutable filesystem inputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', 'node_modules', '.venv', '__pycache__', '.DS_Store'}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML with explicit duplicate keys rejected, including nested maps."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == 'tag:yaml.org,2002:merge':
                # YAML merge aliases may intentionally be overridden by explicit
                # keys. Check explicit duplicates before SafeLoader flattens them.
                key = ('merge',)
            else:
                key = self.construct_object(key_node, deep=deep)
            try:
                if key in seen:
                    raise ValueError('Duplicate YAML mapping key')
                seen.add(key)
            except TypeError as error:
                raise ValueError('YAML mapping keys must be scalar/hashable') from error
        return super().construct_mapping(node, deep=deep)


def parse_frontmatter(text, *, required=False):
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        if required or text.startswith(('---\n', '---\r\n')):
            raise ValueError('SKILL.md must start with properly closed YAML frontmatter')
        return {}, text
    try:
        data = yaml.load(match[1], Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        # YAML exceptions can include the entire source line (possibly secrets).
        location = f' at frontmatter line {mark.line + 1}' if mark else ''
        raise ValueError(f'Invalid YAML frontmatter{location}') from error
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('Frontmatter must be a YAML mapping')
    return data, text[match.end():]


def frontmatter(path, *, required=False):
    return parse_frontmatter(Path(path).read_text(encoding='utf-8'), required=required)


def parse_skill_md(skill_path):
    path = Path(skill_path) / 'SKILL.md'
    data, _ = frontmatter(path)
    return data.get('name', Path(skill_path).name), data.get('description', ''), path.read_text(encoding='utf-8')


def set_description(skill_path, description):
    path = Path(skill_path) / 'SKILL.md'
    data, body = frontmatter(path)
    data['description'] = description
    path.write_text('---\n' + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + '---\n' + body, encoding='utf-8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hash(path):
    h = hashlib.sha256()
    if Path(path).is_symlink():
        raise ValueError(f'Symlink in snapshot: {path}')
    for p in sorted(Path(path).rglob('*')):
        if p.is_symlink():
            raise ValueError(f'Symlink in snapshot: {p}')
        if p.is_file():
            h.update(p.relative_to(path).as_posix().encode() + b'\0' + p.read_bytes() + b'\0')
    return h.hexdigest()


def copy_tree(source, target, *, skill=False, readonly=False):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target == source or source in target.parents:
        raise ValueError('Copy destination cannot be inside source')
    if target.exists():
        raise FileExistsError(target)
    target.mkdir(parents=True)
    for directory, dirs, files in os.walk(source):
        rel = Path(directory).relative_to(source)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (skill and rel == Path('.') and d in {'evals', 'tests'}))
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                raise ValueError(f'Symlinks are not permitted in snapshots: {Path(directory) / name}')
        for name in files:
            if name in EXCLUDED or name.endswith('.pyc'):
                continue
            src, dst = Path(directory) / name, target / rel / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if readonly:
                dst.chmod(0o555 if os.access(src, os.X_OK) else 0o444)
    return tree_hash(target)


def safe_id(value):
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError(f'Unsafe ID: {value!r}')
    return value


def load_evals(path, trigger=False):
    data = read_json(path)
    rows = data if isinstance(data, list) else data['evals']
    seen = set()
    result = []
    for index, item in enumerate(rows):
        item = dict(item)
        item['id'] = safe_id(item.get('id', index + 1))
        if item['id'] in seen:
            raise ValueError(f'Duplicate eval ID: {item["id"]}')
        seen.add(item['id'])
        item['prompt'] = item.get('prompt', item.get('query'))
        if not isinstance(item['prompt'], str) or not item['prompt'].strip():
            raise ValueError('Each eval needs a nonempty prompt')
        item['expectations'] = item.get('expectations', item.get('assertions', []))
        if not isinstance(item['expectations'], list) or any(not isinstance(e, str) for e in item['expectations']):
            raise ValueError('expectations must be a list of strings')
        if trigger and type(item.get('should_trigger')) is not bool:
            raise ValueError('Trigger evals require boolean should_trigger')
        result.append(item)
    if not result:
        raise ValueError('Empty eval set')
    return result


def parse_model(value):
    provider, sep, rest = value.partition('/')
    model, _, variant = rest.partition('#')
    if not sep or not provider or not model:
        raise ValueError('Model must be provider/model[#variant]')
    return {'providerID': provider, 'id': model, **({'variant': variant} if variant else {})}


def parse_json_response(text):
    text = text.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines[-1].strip() == '```':
            text = '\n'.join(lines[1:-1])
    return json.loads(text)


def snapshot_fixtures(rows, skill, workspace):
    """Freeze all fixture bytes before any workers or model calls are started."""
    from scripts.inputs import copy_inputs
    files, hashes = {}, {}
    for row in rows:
        files[row['id']], hashes[row['id']] = [], {}
        originals = row.get('files', [])
        copied = copy_inputs(originals, Path(workspace) / 'snapshots' / 'fixtures' / row['id'], base=skill, readonly=True)
        for original, entry in zip(originals, copied):
            files[row['id']].append({'source': entry['copied'], 'destination': entry['destination'], 'role': entry['role']}
                                    if isinstance(original, dict) else Path(entry['copied']))
            hashes[row['id']][entry['destination']] = entry['sha256']
    return files, hashes
