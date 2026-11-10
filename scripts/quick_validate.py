#!/usr/bin/env python3
"""Strict Agent Skills validation; independent of live OpenCode compatibility."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

if __name__ == '__main__':
    # A validation command must not mutate the installed skill by caching imports.
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.utils import parse_frontmatter

REVISION = '83021e2be457a9b6679148719c1f6074f0ad975e'
ALLOWED_FIELDS = {'name', 'description', 'license', 'compatibility', 'metadata', 'allowed-tools'}
EXIT_CODES = {'PASS': 0, 'FAIL': 1, 'NOT_RUN': 2, 'ERROR': 3}


def issue(field, code, message, **details):
    return {'field': field, 'code': code, 'message': message, **details}


def validate_metadata(data, directory_name):
    """Validate parsed values without mutating, trimming or coercing the input."""
    issues = []
    for key in data:
        if not isinstance(key, str):
            issues.append(issue('frontmatter', 'key_type', 'Top-level keys must be strings', actual_type=type(key).__name__))
        elif key not in ALLOWED_FIELDS:
            issues.append(issue(key, 'unknown_field', 'Unsupported top-level field; use appropriate string metadata'))
    for key in ['name', 'description']:
        if key not in data:
            issues.append(issue(key, 'required', 'Required field is missing'))
        elif not isinstance(data[key], str):
            issues.append(issue(key, 'type', 'Must be a string', actual_type=type(data[key]).__name__))
        elif not data[key].strip():
            issues.append(issue(key, 'empty', 'Must be a nonempty, non-whitespace string'))

    name = data.get('name')
    if isinstance(name, str) and name.strip():
        if len(name) > 64:
            issues.append(issue('name', 'length', 'Must contain 1–64 Unicode characters', actual_length=len(name), maximum=64))
        if name != name.lower() or not all(c.isalnum() or c == '-' for c in name):
            issues.append(issue('name', 'name_format', 'Only lowercase Unicode alphanumeric characters and hyphens are allowed'))
        if name.startswith('-') or name.endswith('-') or '--' in name:
            issues.append(issue('name', 'hyphens', 'No leading/trailing or consecutive hyphens'))
        if name != directory_name:
            issues.append(issue('name', 'directory_mismatch', 'Name must exactly match the parent directory', expected=directory_name))
    description = data.get('description')
    if isinstance(description, str) and len(description) > 1024:
        issues.append(issue('description', 'length', 'Must contain at most 1024 Unicode characters', actual_length=len(description), maximum=1024))

    for key in ['license', 'compatibility', 'allowed-tools']:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str):
            issues.append(issue(key, 'type', 'Must be a string', actual_type=type(value).__name__))
        elif key == 'compatibility':
            if not value.strip():
                issues.append(issue(key, 'empty', 'When present, must describe an actual environment requirement'))
            if not 1 <= len(value) <= 500:
                issues.append(issue(key, 'length', 'When present, must contain 1–500 Unicode characters', actual_length=len(value), maximum=500))
    if 'metadata' in data:
        metadata = data['metadata']
        if not isinstance(metadata, dict):
            issues.append(issue('metadata', 'type', 'Must be a string-to-string mapping', actual_type=type(metadata).__name__))
        else:
            for key, value in metadata.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    issues.append(issue('metadata', 'entry_type', 'Every key and value must be a string',
                                        key_type=type(key).__name__, value_type=type(value).__name__))
    return issues


def recommendations(data, text, body):
    result = []
    name = data.get('name')
    if isinstance(name, str) and not re.fullmatch(r'[a-z0-9]+(-[a-z0-9]+)*', name):
        result.append('Prefer ASCII lowercase kebab-case for portable new identifiers; valid Unicode remains permitted')
    if len(text.splitlines()) >= 500:
        result.append('Consider resource organization for a long SKILL.md; do not shorten useful content without approval')
    if not body.strip():
        result.append('No operating instructions are present; metadata conformance alone does not establish usefulness')
    for key in ['license', 'allowed-tools']:
        if isinstance(data.get(key), str) and not data[key].strip():
            result.append(f'{key} is empty; include optional fields only when meaningful')
    return result


def validate_standard(skill_path):
    """Return this static check's result, never a combined acceptance verdict."""
    root = Path(skill_path)
    result = {'schema_version': 1, 'check': 'agent_skills', 'contract_revision': REVISION,
              'status': 'NOT_RUN', 'issues': [], 'recommendations': [], 'evidence': [],
              'skill_md_sha256': None}
    try:
        try:
            root_info = root.stat()
        except FileNotFoundError:
            root_info = None
        if root_info is None or not stat.S_ISDIR(root_info.st_mode):
            result['status'] = 'FAIL'
            result['issues'] = [issue('directory', 'missing_directory', 'Skill path must be an existing directory')]
            return result
        path = root / 'SKILL.md'
        names = {entry.name for entry in root.iterdir()}
        try:
            file_info = path.stat() if 'SKILL.md' in names else None
        except FileNotFoundError:
            file_info = None
        if file_info is None or not stat.S_ISREG(file_info.st_mode):
            result['status'] = 'FAIL'
            result['issues'] = [issue('SKILL.md', 'missing_file', 'A regular SKILL.md file is required')]
            return result
        raw = path.read_bytes()
        result['skill_md_sha256'] = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode('utf-8')
            data, body = parse_frontmatter(text, required=True)
        except (ValueError, RecursionError) as error:
            result['status'] = 'FAIL'
            message = 'Invalid UTF-8 encoding' if isinstance(error, UnicodeError) else 'Recursive YAML exceeds parser limits' if isinstance(error, RecursionError) else str(error)
            result['issues'] = [issue('frontmatter', 'parse', message)]
            return result
        result['issues'] = validate_metadata(data, Path(os.path.abspath(root)).name)
        literal_root = Path(os.path.abspath(root))
        if literal_root.name not in {entry.name for entry in literal_root.parent.iterdir()}:
            result['issues'].append(issue('name', 'directory_spelling', 'Skill path must use the exact directory-entry spelling; filesystem aliases do not establish a name match'))
        result['recommendations'] = recommendations(data, text, body)
        result['status'] = 'FAIL' if result['issues'] else 'PASS'
        result['evidence'] = [{'file': 'SKILL.md', 'sha256': result['skill_md_sha256']}]
    except OSError as error:
        result['status'] = 'ERROR'
        result['issues'] = [issue('SKILL.md', 'io', 'Unable to read the skill artifact', error_type=type(error).__name__, errno=error.errno)]
    return result


def validate_opencode_static(skill_path):
    """Legacy static loader check, explicitly not a live compatibility verdict."""
    try:
        from scripts.utils import frontmatter
        data, body = frontmatter(Path(skill_path) / 'SKILL.md')
        errors = [issue(key, 'type', 'Must be a string') for key in ['name', 'description']
                  if key in data and not isinstance(data[key], str)]
        if not body.strip():
            errors.append(issue('body', 'empty', 'Skill body is empty'))
        return {'check': 'opencode_static', 'status': 'FAIL' if errors else 'PASS', 'issues': errors,
                'opencode_compatibility': 'NOT_RUN'}
    except OSError:
        return {'check': 'opencode_static', 'status': 'ERROR', 'issues': [issue('SKILL.md', 'io', 'Unable to read artifact')], 'opencode_compatibility': 'NOT_RUN'}
    except ValueError as error:
        return {'check': 'opencode_static', 'status': 'FAIL', 'issues': [issue('frontmatter', 'parse', str(error))], 'opencode_compatibility': 'NOT_RUN'}


def validate_skill(skill_path):
    """Backward-compatible tuple interface, now enforcing the strict standard."""
    result = validate_standard(skill_path)
    message = 'Agent Skills: ' + result['status']
    if result['issues']:
        message += '; ' + '; '.join(f'{i["field"]}: {i["message"]}' for i in result['issues'])
    return result['status'] == 'PASS', message


def lint(skill_path):
    return validate_standard(skill_path)['recommendations']


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='Exit codes: 0 PASS, 1 FAIL, 2 NOT_RUN, 3 ERROR. This command checks Agent Skills only; it does not certify OpenCode compatibility.')
    parser.add_argument('skill', type=Path)
    parser.add_argument('--lint', action='store_true', help='Also print nonblocking authoring recommendations')
    parser.add_argument('--json', action='store_true', help='Emit this check as structured JSON')
    args = parser.parse_args()
    result = validate_standard(args.skill)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('Agent Skills:', result['status'])
        for row in result['issues']:
            details = ', '.join(f'{k}={v}' for k, v in row.items() if k not in {'field', 'code', 'message'})
            print(f'{row["field"]} [{row["code"]}]: {row["message"]}' + (f' ({details})' if details else ''))
        if args.lint:
            for message in result['recommendations']:
                print('Recommendation:', message)
    return EXIT_CODES[result['status']]


if __name__ == '__main__':
    sys.exit(main())
