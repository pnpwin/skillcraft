"""Narrow observation-only exception for an immutable oversized description."""
from pathlib import Path

from scripts.compliance import skill_hash
from scripts.quick_validate import validate_standard
from scripts.utils import copy_tree, digest, read_json, write_json


def oversized_only(skill):
    check = validate_standard(skill)
    return (check['status'] == 'FAIL' and len(check['issues']) == 1
            and check['issues'][0]['field'] == 'description'
            and check['issues'][0]['code'] == 'length'
            and check['issues'][0]['actual_length'] > 1024)


def freeze(skill, destination):
    if not oversized_only(skill):
        raise ValueError('Historical baseline requires exactly one violation: description >1024 characters')
    destination = Path(destination)
    copy_tree(skill, destination, skill=True, readonly=True)
    manifest = destination.parent / 'historical-baseline.json'
    write_json(manifest, {'schema_version': 1, 'role': 'immutable_oversized_historical_baseline',
        'eligible_for_promotion': False, 'skill_id': destination.name,
        'snapshot': str(destination.resolve()), 'skill_hash': skill_hash(destination),
        'skill_md_sha256': digest(destination / 'SKILL.md'), 'standard': validate_standard(destination)})
    return manifest


def verify(skill, manifest):
    record = read_json(manifest)
    original = Path(record['snapshot'])
    if (record.get('role') != 'immutable_oversized_historical_baseline'
            or record.get('eligible_for_promotion') is not False
            or Path(skill).name != record['skill_id']
            or skill_hash(skill) != record['skill_hash']
            or skill_hash(original) != record['skill_hash']
            or digest(original / 'SKILL.md') != record['skill_md_sha256']
            or not oversized_only(skill)):
        raise ValueError('Historical baseline changed or is not an oversized-only observation artifact')
    return record
