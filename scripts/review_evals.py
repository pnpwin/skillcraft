#!/usr/bin/env python3
"""Render the upstream trigger-set editor with safe embedding and stable IDs."""
import argparse
import html
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.utils import ROOT, load_evals, parse_skill_md


def render(skill, evals):
    name, description, _ = parse_skill_md(skill)
    rows = load_evals(evals, trigger=True)
    encoded = json.dumps(rows, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = (ROOT / 'assets' / 'eval_review.html').read_text()
    return template.replace('__SKILL_NAME_PLACEHOLDER__', html.escape(name)).replace('__SKILL_DESCRIPTION_PLACEHOLDER__', html.escape(description)).replace('__EVAL_DATA_PLACEHOLDER__', encoded)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--skill', type=Path, required=True)
    p.add_argument('--evals', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.write_text(render(a.skill, a.evals), encoding='utf-8')
