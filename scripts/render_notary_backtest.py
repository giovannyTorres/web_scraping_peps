"""Render an offline Spanish backtest report with inspectable SVG artifacts.

Input is a report bundle, never article bodies. Discovery and direct-reference
arms stay separate. Missing measurements are N/D, not zero. All input strings
are escaped; the small fixed script filters existing table rows using textContent.
"""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import hashlib
import html
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_ROWS = 5000
LOCAL_EXTENSIONS = {'.json', '.csv', '.md', '.txt', '.svg', '.png', '.pdf', '.html', '.zip'}
ARM_LABELS = {'native': 'Descubrimiento por nombre', 'native_discovery': 'Descubrimiento por nombre',
              'direct_reference': 'Descarga de referencias conocidas',
              'reference': 'Descarga de referencias conocidas',
              'reference_download': 'Descarga de referencias conocidas'}
PERCENT_METRICS = ('precision', 'precisión', 'recall', 'exhaustividad', 'accuracy',
                   'exactitud', 'specificity', 'especificidad', 'f1', 'cobertura de decisión')


def _text(value: Any, maximum: int = 1200) -> str:
    if value is None:
        return 'N/D'
    if isinstance(value, bool):
        return 'Sí' if value else 'No'
    if isinstance(value, float) and not math.isfinite(value):
        return 'N/D'
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    result = str(value)
    return result if len(result) <= maximum else result[:maximum] + '… [texto abreviado]'


def _escape(value: Any, maximum: int = 1200) -> str:
    return html.escape(_text(value, maximum), quote=True)


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value: Any) -> str:
    if not _numeric(value):
        return _text(value, 120)
    if float(value).is_integer():
        return f'{int(value):,}'.replace(',', '\u202f')
    return f'{value:,.3f}'.rstrip('0').rstrip('.').replace(',', '\u202f').replace('.', ',')


def _value(value: Any, metric: str = '') -> str:
    if _numeric(value) and 0 <= value <= 1 and any(part in metric.lower() for part in PERCENT_METRICS):
        return f'{value * 100:.1f}'.replace('.', ',') + ' %'
    return _number(value)


def _arm(value: Any) -> str:
    return ARM_LABELS.get(str(value), _text(value, 150)) if value not in (None, '') else 'Brazo sin declarar'


def _rows(bundle: dict[str, Any], key: str) -> list[dict[str, Any]]:
    result = bundle.get(key, [])
    if not isinstance(result, list) or len(result) > MAX_ROWS or any(not isinstance(row, dict) for row in result):
        raise ValueError(key + ' must be a bounded array of objects')
    return result


def safe_link(value: Any, output_dir: Path, allowed_local_roots: list[Path] | None = None) -> str | None:
    """HTTPS or an existing allowlisted local artifact; never executable schemes."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme:
        if parsed.scheme.lower() != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return None
        return value
    if parsed.netloc or value.startswith('//') or '\\' in value:
        return None
    if parsed.query or not parsed.path:
        return None
    decoded = unquote(parsed.path)
    if any(ord(character) < 32 for character in decoded) or ':' in decoded:
        return None
    candidate = Path(decoded)
    candidate = candidate if candidate.is_absolute() else output_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
        roots = [output_dir.resolve(), *(Path(root).resolve() for root in allowed_local_roots or [])]
        if (not resolved.is_file() or resolved.suffix.lower() not in LOCAL_EXTENSIONS
                or not any(resolved.is_relative_to(root) for root in roots)):
            return None
        # A report may link a sibling only when its root is explicitly allowed.
        import os
        relative = Path(os.path.relpath(resolved, output_dir.resolve())).as_posix()
    except (ValueError, OSError, RuntimeError):
        return None
    return quote(relative, safe='/') + ('#' + quote(parsed.fragment, safe='') if parsed.fragment else '')


def _fonts() -> str:
    rules = []
    for name, family, weight in (('Lato-Regular.woff2', 'Lato', 400),
                                  ('Lato-Bold.woff2', 'Lato', 700),
                                  ('SourceSerif4-Roman.woff2', 'Source Serif 4', 400)):
        path = ROOT / 'peps' / 'web' / 'fonts' / name
        if path.is_file() and path.stat().st_size <= 1_000_000:
            encoded = base64.b64encode(path.read_bytes()).decode('ascii')
            rules.append("@font-face{font-family:'" + family + "';font-style:normal;font-weight:" +
                         str(weight) + ";font-display:swap;src:url(data:font/woff2;base64," + encoded + ") format('woff2')}")
    return '\n'.join(rules)


def _interval(row: dict[str, Any]) -> tuple[float, float] | None:
    interval = row.get('interval', row.get('wilson95'))
    low = interval.get('lower') if isinstance(interval, dict) else row.get('ci_low')
    high = interval.get('upper') if isinstance(interval, dict) else row.get('ci_high')
    if _numeric(low) and _numeric(high) and 0 <= low <= high <= 1:
        return float(low), float(high)
    return None


def _plots(bundle: dict[str, Any], output: Path) -> list[dict[str, Any]]:
    """Standard matplotlib SVGs are shareable files and embedded offline copies."""
    volume = _rows(bundle, 'volume_rows')
    metric_rows = _rows(bundle, 'metric_rows')
    eligible_volume = [row for row in volume if any(_numeric(row.get(key)) for key in ('candidates', 'texts', 'relevant_texts'))]
    eligible_metrics = [row for row in metric_rows if _numeric(row.get('value')) and 0 <= row['value'] <= 1
                        and any(token in str(row.get('metric', '')).lower() for token in PERCENT_METRICS)]
    if not eligible_volume and not eligible_metrics:
        return []
    try:
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import pyplot as plt
    except ImportError:
        return [{'kind': 'unavailable', 'note': 'No se generaron gráficas: matplotlib no está instalado.'}]
    plt.rcParams.update({'svg.fonttype': 'path', 'svg.hashsalt': 'webscope-notary-backtest',
                         'font.family': 'DejaVu Sans', 'font.size': 9,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.edgecolor': '#c6d6df', 'axes.labelcolor': '#183c53',
                         'text.color': '#183c53', 'xtick.color': '#33586b', 'ytick.color': '#33586b',
                         'figure.facecolor': '#ffffff', 'axes.facecolor': '#ffffff',
                         'text.usetex': False})
    # User-supplied labels are ordinary text, including dollar signs and
    # backslashes. They must never become TeX/mathtext instructions.
    if 'text.parse_math' in plt.rcParams:
        plt.rcParams['text.parse_math'] = False
    artifacts = []
    if eligible_volume:
        groups = defaultdict(list)
        for row in eligible_volume:
            groups[_arm(row.get('arm'))].append(row)
        # Each arm gets a separate chart. No bar is a sum of discovery and
        # acquisition from already-known references.
        for index, (arm, rows) in enumerate(groups.items()):
            rows = sorted(rows, key=lambda row: row.get('texts') if _numeric(row.get('texts')) else -1,
                          reverse=True)
            shown = rows[:60]
            height = max(3.2, .29 * len(shown) + 1.6)
            fig, ax = plt.subplots(figsize=(10.5, height))
            labels = [_text(row.get('name', row.get('entity_id')), 65) for row in shown]
            y = list(range(len(shown)))
            for offset, field, label, color in ((-.25, 'candidates', 'Candidatos', '#b8d5e5'),
                                                 (0, 'texts', 'Textos conservados', '#0060a8'),
                                                 (.25, 'relevant_texts', 'Textos pertinentes revisados', '#028484')):
                points = [(position, row[field]) for position, row in enumerate(shown)
                          if _numeric(row.get(field)) and row[field] >= 0]
                if points:
                    ax.barh([position + offset for position, unused in points],
                            [value for unused, value in points], height=.22, color=color, label=label)
            ax.set_yticks(y, labels)
            ax.invert_yaxis()
            ax.set_xlabel('URLs o candidatos únicos por entidad; no se suman entre brazos')
            ax.set_title(arm + '\nVolumen por entidad', loc='left', fontweight='bold', pad=15)
            ax.xaxis.grid(True, alpha=.18)
            ax.set_axisbelow(True)
            ax.legend(loc='upper left', bbox_to_anchor=(0, -0.06 if len(shown) > 8 else -.13),
                      ncol=2, frameon=False)
            fig.tight_layout(pad=1.7)
            path = output.with_name(output.stem + f'-volumen-{index + 1}.svg')
            fig.savefig(path, format='svg', metadata={'Date': None, 'Title': 'Volumen: ' + arm})
            plt.close(fig)
            artifacts.append({'kind': 'volume', 'path': path, 'title': arm,
                              'note': f'{len(shown)} de {len(rows)} entidades del brazo; las celdas N/D no se dibujan como cero.'})
    if eligible_metrics:
        rows = eligible_metrics[:60]
        fig, ax = plt.subplots(figsize=(10.5, max(3.2, .42 * len(rows) + 1.3)))
        for index, row in enumerate(rows):
            value, interval = row['value'], _interval(row)
            ax.plot([value], [index], marker='o', color='#0060a8', markersize=5)
            if interval:
                ax.hlines(index, interval[0], interval[1], color='#0060a8', linewidth=2)
        ax.set_yticks(range(len(rows)), [_arm(row.get('arm')) + ' · ' + _text(row.get('metric'), 95) for row in rows])
        ax.invert_yaxis()
        ax.set_xlim(-.03, 1.03)
        from matplotlib.ticker import PercentFormatter
        ax.xaxis.set_major_formatter(PercentFormatter(1.0))
        ax.xaxis.grid(True, alpha=.18)
        ax.set_axisbelow(True)
        ax.set_xlabel('Proporción; líneas = intervalo suministrado, no estimado por el renderer')
        ax.set_title('Calidad documental por brazo', loc='left', fontweight='bold', pad=15)
        fig.tight_layout(pad=1.7)
        path = output.with_name(output.stem + '-calidad.svg')
        fig.savefig(path, format='svg', metadata={'Date': None, 'Title': 'Calidad documental por brazo'})
        plt.close(fig)
        artifacts.append({'kind': 'quality', 'path': path, 'title': 'Calidad y sus intervalos',
                          'note': 'Sólo se dibujan proporciones observadas. N/D y métricas sin denominador no se convierten en cero.'})
    return artifacts


def _figure(artifact: dict[str, Any]) -> str:
    if artifact['kind'] == 'unavailable':
        return '<p class="empty">' + _escape(artifact['note']) + '</p>'
    encoded = base64.b64encode(artifact['path'].read_bytes()).decode('ascii')
    return ('<figure class="chart"><img src="data:image/svg+xml;base64,' + encoded + '" alt="' +
            _escape(artifact['title']) + '"><figcaption>' + _escape(artifact['note']) +
            ' <a href="' + quote(artifact['path'].name) + '">Abrir SVG</a></figcaption></figure>')


def _table(headers: tuple[str, ...], rows: list[str], empty: str) -> str:
    if not rows:
        return '<p class="empty">' + _escape(empty) + '</p>'
    return '<div class="table-scroll"><table><thead><tr>' + ''.join('<th scope="col">' + _escape(head) + '</th>' for head in headers) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'


def _alert(bundle: dict[str, Any]) -> str:
    for row in _rows(bundle, 'findings'):
        if str(row.get('severity', '')).lower() in {'critical', 'high', 'error', 'warning', 'alta', 'crítico', 'critico'}:
            return ('<aside class="notice" aria-label="Hallazgo principal"><b>' + _escape(row.get('title'), 250) +
                    '</b><p>' + _escape(row.get('implication', row.get('evidence')), 700) + '</p></aside>')
    for row in _rows(bundle, 'metric_rows'):
        if ('recall' in str(row.get('metric', '')).lower() and _numeric(row.get('value')) and row['value'] < .5):
            return ('<aside class="notice"><b>La recuperación útil es limitada en este corte.</b><p>' +
                    _escape(_arm(row.get('arm'))) + ': recall de ' + _escape(_value(row['value'], 'recall')) +
                    '. Revisa los artículos que faltan y el denominador antes de interpretar la precisión.</p></aside>')
    return ('<aside class="notice neutral"><b>Cobertura y calidad se leen por separado.</b>'
            '<p>Una descarga desde una URL ya conocida no acredita que la búsqueda por nombre haya encontrado esa nota.</p></aside>')


CSS = """
:root{--navy:#072b45;--blue:#0060a8;--teal:#028484;--ink:#193d53;--muted:#5a7180;--line:#dbe5eb;--bg:#f2f6f8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Lato,Arial,sans-serif}a{color:#005597;text-underline-offset:3px}button,input,select{font:inherit}button{cursor:pointer}.wrap{max-width:1320px;padding:0 32px;margin:auto}.brandbar{background:var(--navy);color:white}.brandbar .wrap{display:flex;align-items:center;justify-content:space-between;padding-top:15px;padding-bottom:15px}.brand{font-weight:700;font-size:20px;letter-spacing:-.6px}.brand small{font-size:11px;letter-spacing:1.3px;font-weight:400;margin-left:18px;opacity:.8}.print{background:transparent;color:white;border:1px solid #7998ad;border-radius:7px;padding:7px 13px}header{padding:35px 0 17px}.eyebrow{text-transform:uppercase;letter-spacing:1.7px;font-size:11px;font-weight:700;color:#537384}h1,h2,h3{color:var(--navy)}h1{font:400 clamp(29px,3.8vw,43px)/1.15 'Source Serif 4',Georgia,serif;margin:12px 0 14px;max-width:1000px}h2{font:400 27px/1.2 'Source Serif 4',Georgia,serif;margin:0 0 13px}h3{font-size:16px;margin:0 0 9px}.scope{max-width:1000px;color:var(--muted);font-size:16px}.notice{margin:18px 0 24px;background:#fff5e7;border:1px solid #f0d3a9;border-left:4px solid #ad641d;padding:17px 20px;border-radius:8px}.notice b{color:#76410d;font-size:17px}.notice p{margin:4px 0 0;max-width:1120px}.notice.neutral{background:#eaf3f8;border-color:#c9deeb;border-left-color:var(--blue)}.notice.neutral b{color:var(--navy)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin:0 0 25px}.card,.panel-block{background:white;border:1px solid var(--line);border-radius:10px}.card{padding:18px 20px}.card-label{font-size:12px;color:var(--muted);display:block}.card-value{font-size:29px;font-weight:700;color:var(--navy);margin:4px 0;display:block;overflow-wrap:anywhere}.card-note{font-size:12px;color:var(--muted);margin:6px 0 0}.tabs{display:flex;gap:6px;border-bottom:1px solid #bfd1dc;margin:0 0 22px;overflow-x:auto}.tabs button{border:0;border-bottom:3px solid transparent;padding:12px 18px;white-space:nowrap;color:var(--muted);background:transparent}.tabs button[aria-selected=true]{border-bottom-color:var(--blue);color:var(--blue);font-weight:700}.panel[hidden]{display:none}.panel-block{padding:24px;margin:0 0 20px}.subhead{color:var(--muted);margin:0 0 18px;font-size:14px}.badge{display:inline-block;background:#e9f3f9;border:1px solid #d1e5f0;color:#154e73;border-radius:4px;padding:2px 7px;font-size:11px;font-weight:700}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;text-align:left;font-size:13px}th{color:#466477;font-size:11px;letter-spacing:.3px;background:#f3f7f9;white-space:nowrap}th,td{padding:12px 13px;border-bottom:1px solid #e2eaf0;vertical-align:top}tbody tr:last-child td{border-bottom:0}td:first-child{font-weight:700}td small{display:block;color:var(--muted);font-weight:400;font-size:11px}.numeric{font-variant-numeric:tabular-nums;font-weight:700;white-space:nowrap}.note-cell{min-width:220px;max-width:450px;color:var(--muted);font-size:12px}.empty{background:#f3f7f9;border:1px dashed #c7d7e2;border-radius:7px;padding:20px;color:var(--muted)}.cohort{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:0}.cohort dt{font-size:12px;color:var(--muted)}.cohort dd{margin:2px 0 0;font-size:15px;overflow-wrap:anywhere}.controls{display:flex;align-items:end;gap:14px;flex-wrap:wrap;margin:17px 0}.controls label{font-size:12px;color:var(--muted);flex:1;min-width:180px}.controls input,.controls select{display:block;width:100%;margin-top:5px;padding:10px 12px;border:1px solid #a9becb;border-radius:6px;background:white;color:var(--ink)}.row-count{font-size:12px;color:var(--muted);min-width:145px;padding-bottom:10px}.chart{margin:18px 0 4px;overflow:auto}.chart img{display:block;width:100%;min-width:600px;height:auto}figcaption{font-size:12px;color:var(--muted);padding:9px 0}.matrix-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}.matrix{border:1px solid var(--line);border-radius:7px;padding:16px}.matrix table{margin-top:10px}.matrix table td,.matrix table th{padding:8px}.matrix table td:first-child{font-weight:400}.findings{display:grid;gap:14px}.finding{border-left:3px solid #b4cbd9;padding:4px 0 7px 17px}.finding[data-severity=high],.finding[data-severity=critical],.finding[data-severity=warning]{border-color:#b97a31}.finding p{margin:4px 0 9px;color:var(--muted);font-size:14px}.finding .implication{color:var(--ink)}.limitations{padding-left:20px;color:var(--muted)}.limitations li{padding:4px 0}.links{display:flex;gap:10px;flex-wrap:wrap}.links a{border:1px solid #cbdce7;background:#f3f8fb;border-radius:6px;padding:8px 12px;font-size:13px}.blocked-link{font-size:12px;color:var(--muted);padding:8px 0}.footer{color:var(--muted);font-size:12px;padding:15px 0 35px}.audit-hash{font:11px/1.5 monospace;overflow-wrap:anywhere}tr[hidden]{display:none}:focus-visible{outline:3px solid #65b5df;outline-offset:3px}.js-only{display:none}.js-ready .js-only{display:flex}
@media(max-width:700px){.wrap{padding:0 16px}.brandbar .wrap{padding:13px 16px}.brand small{display:none}header{padding-top:22px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.card{padding:14px}.card-value{font-size:25px}.panel-block{padding:17px}.tabs button{padding:10px 12px}.scope{font-size:14px}.notice{padding:13px 15px}th,td{padding:10px}.chart img{min-width:650px}}
@media print{body{background:white;font-size:11px}.brandbar,.tabs,.controls,.print{display:none!important}.wrap{max-width:none;padding:0}.panel[hidden]{display:block}.panel-block{break-inside:avoid;border:0;padding:0;margin-top:24px}.cards{grid-template-columns:repeat(3,1fr)}.chart{overflow:visible}.chart img{min-width:0}tr[hidden]{display:table-row}.table-scroll{overflow:visible}a{color:inherit}.footer{padding-top:15px}}
"""

SCRIPT = """
document.documentElement.classList.add('js-ready');
const tabs=Array.from(document.querySelectorAll('[data-tab]'));
function selectTab(button){
 tabs.forEach(item=>{item.setAttribute('aria-selected',String(item===button));item.tabIndex=item===button?0:-1;});
 document.querySelectorAll('.panel').forEach(panel=>{panel.hidden=panel.id!=='panel-'+button.dataset.tab;});
}
tabs.forEach((button,index)=>{button.addEventListener('click',()=>selectTab(button));button.addEventListener('keydown',event=>{
 let next=null;if(event.key==='ArrowRight')next=tabs[(index+1)%tabs.length];if(event.key==='ArrowLeft')next=tabs[(index+tabs.length-1)%tabs.length];if(event.key==='Home')next=tabs[0];if(event.key==='End')next=tabs[tabs.length-1];
 if(next){event.preventDefault();selectTab(next);next.focus();}
});});
const search=document.getElementById('entity-search');
const arm=document.getElementById('entity-arm');
const rows=Array.from(document.querySelectorAll('#entity-table tbody tr'));
function filterRows(){
 const query=search.value.toLocaleLowerCase('es-MX').trim();let visible=0;
 rows.forEach(row=>{const show=(!query||row.textContent.toLocaleLowerCase('es-MX').includes(query))&&(!arm.value||row.dataset.arm===arm.value);row.hidden=!show;if(show)visible++;});
 document.getElementById('visible-rows').textContent=visible+' de '+rows.length+' filas';
}
if(search&&arm){search.addEventListener('input',filterRows);arm.addEventListener('change',filterRows);filterRows();}
document.getElementById('print-report').addEventListener('click',()=>window.print());
"""


def render_report(bundle: dict[str, Any], output: str | Path,
                  allowed_local_roots: list[Path] | None = None, make_plots: bool = True) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ValueError('bundle must be an object')
    output = Path(output).resolve()
    if output.suffix.lower() != '.html':
        raise ValueError('output must be an HTML file')
    output.parent.mkdir(parents=True, exist_ok=True)
    cohort = bundle.get('cohort', {})
    if not isinstance(cohort, dict):
        raise ValueError('cohort must be an object')
    limitations = bundle.get('limitations', [])
    if not isinstance(limitations, list) or len(limitations) > MAX_ROWS or any(not isinstance(item, str) for item in limitations):
        raise ValueError('limitations must be a bounded string array')
    for key in ('summary_cards', 'metric_rows', 'confusion_matrices', 'volume_rows', 'source_rows', 'findings', 'links'):
        _rows(bundle, key)
    artifacts = _plots(bundle, output) if make_plots else []
    cards = ''.join('<div class="card"><span class="card-label">' + _escape(row.get('label'), 150) +
                    '</span><strong class="card-value">' + _escape(_number(row.get('value')), 150) +
                    '</strong><p class="card-note">' + _escape(row.get('note', ''), 500) + '</p></div>'
                    for row in _rows(bundle, 'summary_cards'))
    cohort_html = '<dl class="cohort">' + ''.join('<div><dt>' + _escape(str(key).replace('_', ' '), 150) + '</dt><dd>' +
                                                 _escape(value, 550) + '</dd></div>' for key, value in cohort.items()) + '</dl>'
    metric_rows = []
    for row in _rows(bundle, 'metric_rows'):
        interval = _interval(row)
        interval_html = ('<small>IC: ' + _escape(_value(interval[0], 'precision')) + ' – ' +
                         _escape(_value(interval[1], 'precision')) + '</small>') if interval else ''
        fraction = _number(row.get('numerator')) + ' / ' + _number(row.get('denominator'))
        metric_rows.append('<tr><td><span class="badge">' + _escape(_arm(row.get('arm'))) + '</span></td><td>' +
                           _escape(row.get('metric'), 220) + '</td><td class="numeric">' +
                           _escape(_value(row.get('value'), str(row.get('metric', '')))) + interval_html +
                           '</td><td class="numeric">' + _escape(fraction) + '</td><td class="note-cell">' +
                           _escape(row.get('note', ''), 700) + '</td></tr>')
    metrics_html = _table(('Brazo', 'Métrica', 'Valor', 'Numerador / denominador', 'Alcance'), metric_rows,
                          'No hay métricas calculadas para este corte.')
    matrices = []
    for row in _rows(bundle, 'confusion_matrices'):
        matrices.append('<article class="matrix"><h3>' + _escape(row.get('variant'), 200) + '</h3>' +
                        ('<span class="badge">' + _escape(_arm(row['arm'])) + '</span>' if row.get('arm') else '') +
                        '<table><thead><tr><th scope="col">Referencia revisada</th><th scope="col">Predice sí</th><th scope="col">Predice no</th></tr></thead><tbody>' +
                        '<tr><th scope="row">Adverso reportado</th><td class="numeric">' + _escape(_number(row.get('tp'))) +
                        ' VP</td><td class="numeric">' + _escape(_number(row.get('fn'))) + ' FN</td></tr>' +
                        '<tr><th scope="row">Sin papel adverso</th><td class="numeric">' + _escape(_number(row.get('fp'))) +
                        ' FP</td><td class="numeric">' + _escape(_number(row.get('tn'))) + ' VN</td></tr></tbody></table><p class="card-note">Abstenciones: ' +
                        _escape(_number(row.get('abstain'))) + '. Su tratamiento en cada denominador debe declararse en las métricas.</p></article>')
    volume = _rows(bundle, 'volume_rows')
    arms = list(dict.fromkeys(_arm(row.get('arm')) for row in volume))
    arm_indices = {arm: str(index + 1) for index, arm in enumerate(arms)}
    volume_rows = []
    for row in volume:
        arm = _arm(row.get('arm'))
        volume_rows.append('<tr data-arm="' + arm_indices[arm] + '"><td>' + _escape(row.get('name'), 250) +
                           '<small>' + _escape(row.get('entity_id'), 180) + '</small></td><td><span class="badge">' +
                           _escape(arm) + '</span></td><td>' + _escape(row.get('selection_group'), 160) + '<small>' +
                           _escape(row.get('gold_status'), 200) + '</small></td>' +
                           ''.join('<td class="numeric">' + _escape(_number(row.get(field))) + '</td>' for field in ('candidates', 'texts', 'relevant_texts')) +
                           '<td class="note-cell">' + _escape(row.get('notes', ''), 700) + '</td></tr>')
    volume_html = '<div id="entity-table">' + _table(('Entidad', 'Brazo', 'Selección / referencia', 'Candidatos', 'Textos', 'Pertinentes revisados', 'Detalle'),
                                                     volume_rows, 'No hay filas de volumen por entidad.') + '</div>'
    controls = ('<div class="controls js-only"><label>Buscar nombre, grupo o nota<input id="entity-search" type="search" autocomplete="off" placeholder="Filtrar las filas de esta tabla"></label>' +
                '<label>Brazo<select id="entity-arm"><option value="">Todos, en filas separadas</option>' +
                ''.join('<option value="' + arm_indices[arm] + '">' + _escape(arm) + '</option>' for arm in arms) +
                '</select></label><span id="visible-rows" class="row-count"></span></div>')
    source_rows = ['<tr><td>' + _escape(row.get('source'), 200) + '</td><td><span class="badge">' +
                   _escape(_arm(row.get('arm'))) + '</span></td>' +
                   ''.join('<td class="numeric">' + _escape(_number(row.get(field))) + '</td>' for field in ('queries', 'http', 'candidates', 'texts')) +
                   '<td class="note-cell">' + _escape(row.get('status'), 700) + '</td></tr>' for row in _rows(bundle, 'source_rows')]
    sources_html = _table(('Fuente', 'Brazo', 'Consultas', 'HTTP', 'Candidatos', 'Textos', 'Estado y límites'), source_rows,
                          'No hay mediciones por fuente para este corte.')
    findings = ''.join('<article class="finding" data-severity="' +
                       ('warning' if str(row.get('severity')).lower() in {'warning', 'high', 'critical', 'error', 'alta', 'crítico', 'critico'} else 'info') +
                       '"><h3>' + _escape(row.get('title'), 250) + '</h3><p>' + _escape(row.get('evidence'), 1100) +
                       '</p><p class="implication">' + _escape(row.get('implication'), 800) + '</p></article>'
                       for row in _rows(bundle, 'findings'))
    links = []
    for row in _rows(bundle, 'links'):
        url = safe_link(row.get('url'), output.parent, allowed_local_roots)
        if url:
            remote = url.startswith('https:')
            links.append('<a href="' + html.escape(url, quote=True) + '"' + (' target="_blank" rel="noopener noreferrer"' if remote else '') +
                         '>' + _escape(row.get('label'), 200) + '</a>')
        else:
            links.append('<span class="blocked-link">' + _escape(row.get('label'), 200) + ' · enlace no autorizado</span>')
    bundle_hash = hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True, allow_nan=False,
                                           separators=(',', ':')).encode('utf-8')).hexdigest()
    quality_plots = ''.join(_figure(artifact) for artifact in artifacts if artifact['kind'] in {'quality', 'unavailable'})
    volume_plots = ''.join(_figure(artifact) for artifact in artifacts if artifact['kind'] == 'volume')
    title = _escape(bundle.get('title', 'Backtesting de noticias sobre notarios'), 300)
    body = ('<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
            '<meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; font-src data:; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; connect-src \'none\'; object-src \'none\'; base-uri \'none\'; form-action \'none\'">' +
            '<title>' + title + '</title><style>' + _fonts() + CSS + '</style></head><body><div class="brandbar"><div class="wrap"><div class="brand">WebScope<small>VALIDACIÓN DOCUMENTAL</small></div>' +
            '<button id="print-report" class="print" type="button">Imprimir / PDF</button></div></div><main class="wrap"><header><div class="eyebrow">Backtesting · búsqueda, volumen y calidad</div><h1>' + title +
            '</h1><p class="scope">' + _escape(bundle.get('scope'), 2500) + '</p></header>' + _alert(bundle) +
            '<div class="cards">' + cards + '</div><nav class="tabs" role="tablist" aria-label="Secciones del informe">' +
            ''.join('<button type="button" role="tab" id="tab-' + tab + '" aria-controls="panel-' + tab + '" tabindex="' + ('0' if index == 0 else '-1') + '" data-tab="' + tab + '" aria-selected="' + ('true' if index == 0 else 'false') + '">' + label + '</button>' for index, (tab, label) in enumerate((('overview', 'Lectura y métricas'), ('entities', 'Entidades y volumen'), ('sources', 'Fuentes'), ('findings', 'Hallazgos y respaldo')))) +
            '</nav><section class="panel" id="panel-overview" role="tabpanel" aria-labelledby="tab-overview"><div class="panel-block"><h2>Qué se puso a prueba</h2>' + cohort_html + '</div>' +
            '<div class="panel-block"><h2>Calidad, con denominadores visibles</h2><p class="subhead">N/D significa medición no disponible. Los brazos permanecen separados; sus volúmenes no se suman como descubrimiento.</p>' + metrics_html + quality_plots + '</div>' +
            '<div class="panel-block"><h2>Matrices de confusión</h2><p class="subhead">Son decisiones documentales frente a revisión; no son determinaciones sobre una persona.</p><div class="matrix-grid">' +
            (''.join(matrices) or '<p class="empty">No hay matrices calculadas para este corte.</p>') + '</div></div></section>' +
            '<section class="panel" id="panel-entities" role="tabpanel" aria-labelledby="tab-entities" hidden><div class="panel-block"><h2>Cuántas notas se alcanzaron</h2><p class="subhead">Cada fila mantiene su brazo y grupo de selección. Candidatos, textos y pertinencia revisada son medidas distintas.</p>' + controls + volume_html + volume_plots + '</div></section>' +
            '<section class="panel" id="panel-sources" role="tabpanel" aria-labelledby="tab-sources" hidden><div class="panel-block"><h2>Qué aportó cada fuente</h2><p class="subhead">Las consultas, las peticiones HTTP y los artículos conservados tienen denominadores distintos.</p>' + sources_html + '</div></section>' +
            '<section class="panel" id="panel-findings" role="tabpanel" aria-labelledby="tab-findings" hidden><div class="panel-block"><h2>Hallazgos que cambian la interpretación</h2><div class="findings">' +
            (findings or '<p class="empty">No se suministraron hallazgos para este corte.</p>') + '</div></div><div class="panel-block"><h2>Alcance y límites</h2><ul class="limitations">' +
            ''.join('<li>' + _escape(item, 1200) + '</li>' for item in limitations) + '</ul></div><div class="panel-block"><h2>Respaldo auditable</h2><div class="links">' + ''.join(links) +
            '</div><p class="card-note">SHA-256 del bundle suministrado</p><p class="audit-hash">' + bundle_hash + '</p></div></section>' +
            '<footer class="footer">Procesamiento local · Informe consultable sin conexión. Ninguna etiqueta o coincidencia nominal acredita identidad, culpabilidad o ausencia de incidentes.</footer></main><script>' + SCRIPT + '</script></body></html>')
    output.write_text(body, encoding='utf-8')
    return {'html': str(output), 'bundle_sha256': bundle_hash,
            'svg_artifacts': [str(artifact['path']) for artifact in artifacts if artifact.get('path')],
            'volume_rows': len(volume), 'metric_rows': len(metric_rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-local-root', type=Path, action='append', default=[])
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args(argv)
    if args.bundle.stat().st_size > 32_000_000:
        parser.error('bundle exceeds 32 MB')
    bundle = json.loads(args.bundle.read_text(encoding='utf-8-sig'))
    print(json.dumps(render_report(bundle, args.output, args.allow_local_root, not args.no_plots), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
