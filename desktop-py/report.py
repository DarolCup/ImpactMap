"""Portable report: no scripts, network resources or repository code execution."""
from html import escape

from analysis import AnalysisResult
from result import ResultModel, chain_text, finding_text


def render_report(result: AnalysisResult) -> str:
    model = ResultModel(result)
    title = "Прогноз до изменения" if result.planned else "Проверка изменений"
    rows = "".join(
        f"<tr><td>{escape(path)}</td><td>{item['distance']}</td>"
        f"<td>{escape(chain_text(model.chains.get(path, ())))}</td></tr>"
        for path, item in model.affected.items()
    )
    findings = "".join(f"<li>{escape(' '.join(finding_text(item)))}<pre>{escape(item['evidence'])}</pre></li>"
                       for item in model.findings)
    notes = "".join(f"<li>{escape(note)}</li>" for note in (*result.notes, *model.limitations, *result.response['warnings']))
    selected = ", ".join(sorted(model.changed)) or "Нет"
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ImpactMap 0.0.3</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#172238;background:#f6f8fc}}
table{{border-collapse:collapse;width:100%;background:white}}td,th{{border:1px solid #ccd4e0;padding:12px;text-align:left}}
pre{{white-space:pre-wrap}}td,li,p{{overflow-wrap:anywhere}}h1{{color:#264bbb}}</style>
<h1>ImpactMap 0.0.3 · {title}</h1><p>{escape(result.source)}</p><p>{escape(result.comparison)}</p>
<p>Выбрано / изменено: <b>{escape(selected)}</b></p><p>Потенциально затронуто: <b>{len(model.affected)}</b></p>
<p>A → B: файл A использует B. Влияние изменения распространяется в обратном направлении.</p>
<table><tr><th>Затронутый файл</th><th>Расстояние</th><th>Цепочка к выбранному файлу</th></tr>{rows}</table>
<p>Отсутствие найденных связей не гарантирует отсутствие влияния.</p>
<h2>Находки</h2><ul>{findings}</ul><h2>Ограничения и предупреждения</h2><ul>{notes}</ul></html>'''
