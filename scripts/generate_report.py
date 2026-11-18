#!/usr/bin/env python3
"""Self-contained interactive candidate × query report; no runtime dependencies."""
import argparse
import copy
import html
import json
import re
from pathlib import Path


def esc(value):
    return html.escape(str(value), quote=True)


def load_report(path):
    """Hydrate legacy loop summaries only from owned report paths, never candidate paths."""
    path = Path(path)
    data = json.loads(path.read_text())
    root = path.parent.resolve()
    def read_results(relative):
        p = (root / relative).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            return None
        return json.loads(p.read_text()).get('results')
    for item in data.get('iterations', []):
        i = item.get('iteration')
        if type(i) is not int or i < 0:
            continue
        for split in ['train', 'validation']:
            key = f'{split}_results'
            if key not in item:
                rows = read_results(f'iteration-{i:03d}/{split}/trigger-results.json')
                if rows is not None:
                    item[key] = rows
    if 'final_test_results' not in data:
        rows = read_results('final-test/trigger-results.json')
        if rows is not None:
            data['final_test_results'] = rows
    return data


def cell(rows, repeats, prefix=None):
    if not rows:
        return '<td class="unknown">Not run / no records</td>'
    valid = [r for r in rows if r.get('status') == 'completed' and type(r.get('triggered')) is bool and type(r.get('should_trigger')) is bool]
    correct = sum(r['triggered'] == r['should_trigger'] for r in valid)
    loads = sum(r['triggered'] for r in valid)
    has_error = len(valid) != len(rows)
    cls = 'error' if has_error else 'pass' if correct == len(valid) else 'fail'
    missing = max(0, repeats - len(rows)) if type(repeats) is int else None
    header = f'{correct}/{len(valid)} correct; {loads}/{len(valid)} loaded; {len(rows)-len(valid)} errors'
    if missing:
        header += f'; {missing} missing'
        cls = 'unknown' if not has_error else cls
    parts = [f'<td class="{cls}"><details><summary>{esc(header)}</summary>']
    for r in sorted(rows, key=lambda r: str(r.get('repeat', ''))):
        expected, actual = r.get('should_trigger'), r.get('triggered')
        if r not in valid:
            verdict = 'ERROR / unknown observation'
        elif expected == actual:
            verdict = 'TP' if actual else 'TN'
        else:
            verdict = 'FP' if actual else 'FN'
        parts.append('<p>' + esc(f'Repeat {r.get("repeat", "unknown")}: {verdict}; expected={expected}, actual={actual}; status={r.get("status", "unknown")}') + '</p>')
        if r.get('error'):
            parts.append('<pre>' + esc(json.dumps(r['error'], ensure_ascii=False)) + '</pre>')
        if prefix is not None and re.fullmatch(r'[A-Za-z0-9_-]+', str(r.get('eval_id', ''))) and type(r.get('repeat')) is int and r['repeat'] > 0:
            base = f'{prefix}eval-{r["eval_id"]}/natural/run-{r["repeat"]:03d}/'
            parts.append(f'<p><a href="{esc(base)}transcript.md">Transcript</a> · <a href="{esc(base)}run.json">Run metadata</a></p>')
    return ''.join(parts) + '</details></td>'


def generate_html(data, auto_refresh=False, skill_name=''):
    data = copy.deepcopy(data)
    title = esc(skill_name or 'SkillCraft trigger evaluation')
    iterations = data.get('iterations', [])
    standalone = not iterations and isinstance(data.get('results'), list)
    if standalone:
        iterations = [{'candidate': 'Natural-trigger run', 'natural': data.get('summary', {}), 'natural_results': data['results']}]
    best = data.get('best')
    sections = ['natural'] if standalone else ['train', 'validation', 'final_test']
    body = [f'<h1>{title}</h1>']
    if data.get('harness'):
        body.append(f'<p>Harness: {esc(data["harness"])}</p>')
    if not standalone:
        body.append(f'<p>Selection: validation {esc(data.get("selection_metric", "not recorded"))}.</p>')
    body += [
            '<p>Green = correct classification; red = FP/FN; amber = technical error; gray = missing/unknown. An untested candidate has no final-test score.</p>',
            '<label>Find candidate/query <input id="search" type="search"></label> ',
            '<label>Dataset <select id="split"><option value="all">All</option>' + ''.join(f'<option value="{s}">{s.replace("_", " ")}</option>' for s in sections) + '</select></label> ',
            '<label><input id="selected" type="checkbox">Selected candidate only</label>',
            '<p id="visible-count" aria-live="polite"></p>']
    for split in sections:
        key = f'{split}_results'
        items = iterations
        if split == 'final_test' and data.get('final_test_original_results'):
            items = [*iterations, {'candidate': 'Original description (baseline)', 'original_test': True}]
        datasets = []
        for item in items:
            rows = data.get('final_test_results', []) if split == 'final_test' and item.get('candidate') == best else item.get(key, []) if split != 'final_test' else []
            if item.get('original_test'):
                rows = data['final_test_original_results']
            datasets.append(rows or [])
        ids = sorted({str(r.get('eval_id', 'unknown')) for rows in datasets for r in rows})
        body.append(f'<section data-split="{split}"><h2>{split.replace("_", " ").title()}</h2>')
        if split == 'final_test' and data.get('final_test_balanced_accuracy_delta') is not None:
            delta = data['final_test_balanced_accuracy_delta']
            body.append(f'<p>Balanced accuracy change against the original: {delta:+.1%}</p>')
        if not ids:
            body.append('<p>No per-query records. This dataset was not run, is incomplete, or the legacy report contains summaries only.</p>')
        body.append('<div class="scroll"><table><thead><tr><th>Candidate / description / summary</th>')
        for id_ in ids:
            prompts = list(dict.fromkeys(r.get('prompt', '') for rows in datasets for r in rows if str(r.get('eval_id', 'unknown')) == id_))
            body.append(f'<th>{esc(id_)}<details><summary>Query</summary>{esc(" / ".join(prompts))}</details></th>')
        body.append('</tr></thead><tbody>')
        for item, rows in zip(items, datasets):
            selected = item.get('candidate') == best and best is not None
            summary = data.get('final_test') if split == 'final_test' and selected else item.get(split) if split != 'final_test' else None
            if item.get('original_test'):
                summary = data['final_test_original']
            name = str(item.get('candidate', f'iteration-{item.get("iteration", "unknown")}'))
            search_text = name + ' ' + str(item.get('description', '')) + ' ' + ' '.join(str(r.get('prompt', '')) for r in rows)
            body.append(f'<tr data-selected="{str(selected).lower()}" data-search="{esc(search_text.casefold())}"><th scope="row">{esc(name)}')
            if selected:
                body.append('<strong class="chosen">Selected on validation</strong>')
            body.append('<details><summary>Description and rationale</summary><p>' + esc(item.get('description') or 'Description not recorded (legacy)') + '</p><p>' + esc(item.get('rationale') or 'Rationale not recorded') + '</p><code>' + esc(item.get('snapshot_hash') or 'Hash not recorded') + '</code></details>')
            if isinstance(summary, dict):
                body.append('<dl>' + ''.join(f'<dt>{esc(k)}</dt><dd>{esc(summary.get(k) if summary.get(k) is not None else "unknown")}</dd>' for k in ['precision', 'recall', 'accuracy', 'errors', 'error_rate']) + '</dl>')
                if split == 'validation':
                    body.append('<p>Selection score: ' + esc(item.get('score') if item.get('score') is not None else 'unknown / ineligible') + '</p>')
            else:
                body.append('<p>Not run / summary unavailable</p>')
            body.append('</th>')
            index = item.get('iteration')
            prefix = '' if standalone else 'final-test/' if split == 'final_test' and selected else f'iteration-{index:03d}/{split}/' if type(index) is int and index >= 0 and split != 'final_test' else None
            if item.get('original_test'):
                prefix = 'final-test-original/'
            for id_ in ids:
                body.append(cell([r for r in rows if str(r.get('eval_id', 'unknown')) == id_], data.get('repeats'), prefix))
            body.append('</tr>')
        body.append('</tbody></table></div></section>')
    if data.get('quality_regression') is not None:
        body.append('<details><summary>Quality regression and paired resource deltas</summary><pre>' + esc(json.dumps(data['quality_regression'], ensure_ascii=False, indent=2)) + '</pre></details>')
    body.append('<h2>Evidence</h2><p>Use the report links to inspect each run’s transcript and metadata.</p>')
    body.append('<button id="download">Download source JSON</button><details><summary>Source JSON</summary><pre id="source">' + esc(json.dumps(data, ensure_ascii=False, indent=2)) + '</pre></details>')
    script = '''
function applyFilters() {
  const q = document.querySelector('#search').value.toLowerCase();
  const split = document.querySelector('#split').value;
  const selected = document.querySelector('#selected').checked;
  let count = 0;
  document.querySelectorAll('section[data-split]').forEach(section => {
    section.hidden = split !== 'all' && split !== section.dataset.split;
    section.querySelectorAll('tr[data-search]').forEach(row => {
      row.hidden = (selected && row.dataset.selected !== 'true') || !row.dataset.search.includes(q);
      if (!section.hidden && !row.hidden) count++;
    });
  });
  document.querySelector('#visible-count').textContent = count + ' candidate rows visible';
}
['search','split','selected'].forEach(id => document.getElementById(id).addEventListener('input',applyFilters));
document.getElementById('download').addEventListener('click', () => {
  const url=URL.createObjectURL(new Blob([document.getElementById('source').textContent],{type:'application/json'}));
  const link=document.createElement('a'); link.href=url; link.download='optimization-source.json'; link.click(); URL.revokeObjectURL(url);
});
applyFilters();
'''
    css = 'body{font:16px system-ui;margin:32px;color:#222}table{border-collapse:collapse;width:100%}th,td{border:1px solid #aaa;padding:12px;vertical-align:top;min-width:180px;text-align:left}th{background:#f3f4f6}.scroll{overflow:auto}.pass{background:#e0f3e5}.fail{background:#fde5e5}.error{background:#fff0c2}.unknown{background:#eee}.chosen{display:block;color:#12622d}details{margin:8px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}dt,dd{display:inline;margin:0 6px 0 0}dd:after{content:"";display:block}label{margin-right:16px}[hidden]{display:none!important}section{margin:24px 0}'
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' + ('<meta http-equiv="refresh" content="10">' if auto_refresh else '') + f'<title>{title}</title><style>{css}</style><body>' + ''.join(body) + f'<script>{script}</script></body></html>'


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    a.output.write_text(generate_html(load_report(a.input)), encoding='utf-8')
