"""Evaluacion de meeting_qa contra .claude/evals/meeting_qa (ver baseline.md).

Llama al modelo de verdad: gasta tokens y NO va en la suite unitaria.

    python scripts/eval_meeting_qa.py --version v1 [--model claude-sonnet-5-5] [--runs 3]

Escribe results_<version>.md con PASS/FAIL, latencias y coste, nunca el texto
de las respuestas.
"""
import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import meeting_qa  # noqa: E402

EVAL_DIR = ROOT / '.claude' / 'evals' / 'meeting_qa'
TS = re.compile(r'\[(\d{1,3}):(\d{2})\]')
NULL = re.compile(r'no se (ha hablado|hablo|ha mencionado|menciono|ha dicho|dijo)')
EN_WORDS = (' the ', ' is ', ' on ', ' at ', ' in ', ' will ', ' and ')
ES_WORDS = (' el ', ' la ', ' es ', ' en ', ' de ', ' y ', ' que ')


def norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', s.lower())
    return ''.join(c for c in s if not unicodedata.combining(c))


def secs(mm: str, ss: str) -> int:
    return int(mm) * 60 + int(ss)


def grade(case: dict, answer: str, valid_ts: set[int], upto: int) -> tuple[bool, str]:
    """(pasa, motivo si no pasa). Los criterios estan en baseline.md."""
    cited = [secs(m, s) for m, s in TS.findall(answer)]
    bad = [c for c in cited if c not in valid_ts and c != upto]
    if bad:
        return False, f'minuto inventado {bad[0] // 60:02d}:{bad[0] % 60:02d}'
    text = norm(answer)
    if case['class'] == 'null':
        if not NULL.search(text):
            return False, 'no dice que no se hablo'
        if case.get('no_numbers') and re.search(r'\d', TS.sub('', answer)):
            return False, 'da una cifra'
        return True, ''
    for group in case['groups']:
        if not any(norm(term) in text for term in group):
            return False, f'falta {group[0]!r}'
    expected = [secs(*t.split(':')) for t in case['ts']]
    if not any(abs(c - e) <= 20 for c in cited for e in expected):
        return False, 'sin minuto de donde sale'
    if case.get('lang') == 'en':
        padded = f' {text} '
        if sum(padded.count(w) for w in EN_WORDS) <= sum(padded.count(w) for w in ES_WORDS):
            return False, 'no responde en ingles'
    return True, ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', required=True)
    ap.add_argument('--model', default=meeting_qa.DEFAULT_MODEL)
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--mode', choices=('session', 'oneshot'), default='session',
                    help='session: el camino del panel (sesion abierta antes de preguntar)')
    args = ap.parse_args()

    spec = json.loads((EVAL_DIR / 'cases.json').read_text(encoding='utf-8'))
    lines = (EVAL_DIR / spec['transcript']).read_text(encoding='utf-8').splitlines()
    valid_ts = {secs(m, s) for line in lines for m, s in TS.findall(line[:8])}
    upto = secs(*spec['upto'].split(':'))

    # En el panel la sesion se abre al mostrar la caja de preguntas: cuando se
    # pregunta, el arranque (9,5-12 s) ya paso. Aqui, cada pregunta en una sesion
    # nueva abierta LOOKAHEAD preguntas antes, para medir lo mismo.
    LOOKAHEAD = 5
    pool = []

    def answer(question):
        if args.mode == 'oneshot':
            return meeting_qa.ask(lines, question, me_label='Felipe', model=args.model)
        while len(pool) < LOOKAHEAD:
            pool.append(meeting_qa.Session(model=args.model, me_label='Felipe'))
        session = pool.pop(0)
        pool.append(meeting_qa.Session(model=args.model, me_label='Felipe'))
        try:
            return session.ask(lines, question)
        finally:
            session.close()

    if args.mode == 'session':
        pool.extend(meeting_qa.Session(model=args.model, me_label='Felipe') for _ in range(LOOKAHEAD))
        time.sleep(15)                       # que la primera no mida el arranque

    rows, firsts, totals, costs = [], [], [], []
    for case in spec['cases']:
        results = []
        for _ in range(args.runs):
            r = answer(case['question'])
            ok, why = (False, f"error: {r['error']}") if r['error'] else grade(case, r['text'], valid_ts, upto)
            results.append((ok, why))
            if r['first_text_s'] is not None:
                firsts.append(r['first_text_s'])
            totals.append(r['total_s'])
            if r['cost_usd'] is not None:
                costs.append(r['cost_usd'])
            print(f"{case['id']} {'PASS' if ok else 'FAIL'} {why} ({r['total_s']}s)", flush=True)
        rows.append((case, results))
    for s in pool:
        s.close()

    def passed(results):
        return sum(ok for ok, _ in results) * 2 > len(results)      # mayoria

    answered = [(c, r) for c, r in rows if c['class'] != 'null']
    nulls = [(c, r) for c, r in rows if c['class'] == 'null']
    ok_answered = sum(passed(r) for _, r in answered)
    ok_null_runs = sum(ok for _, r in nulls for ok, _ in r)
    gate_answered = ok_answered >= len(answered) - 1
    gate_null = ok_null_runs == len(nulls) * args.runs

    def p50(xs):
        return round(statistics.median(xs), 1) if xs else None

    out = [f'# meeting_qa {args.version} - {args.model} ({args.mode}), {args.runs} ejecuciones por caso', '',
           '| Caso | Clase | ' + ' | '.join(f'Ej. {i + 1}' for i in range(args.runs)) + ' | Pasa | Motivo |',
           '|---|---|' + '---|' * args.runs + '---|---|']
    for case, results in rows:
        cells = ' | '.join('PASS' if ok else 'FAIL' for ok, _ in results)
        why = '; '.join(sorted({w for ok, w in results if not ok}))
        verdict = ('PASS' if all(ok for ok, _ in results) else 'FAIL') if case['class'] == 'null' \
            else ('PASS' if passed(results) else 'FAIL')
        out.append(f"| {case['id']} | {case['class']} | {cells} | {verdict} | {why} |")
    pass1 = sum(r[0][0] for _, r in answered) / max(1, len(answered))
    out += ['',
            f'- Con respuesta: {ok_answered}/{len(answered)} por mayoria (umbral >= {len(answered) - 1}); '
            f'pass@1 {pass1:.0%} -> {"OK" if gate_answered else "NO LLEGA"}',
            f'- No dichas: {ok_null_runs}/{len(nulls) * args.runs} ejecuciones (umbral todas) -> '
            f'{"OK" if gate_null else "NO LLEGA"}',
            f'- Primer texto p50 {p50(firsts)} s (objetivo <= 8); total p50 {p50(totals)} s (objetivo <= 15)',
            f'- Coste por pregunta: media ${statistics.mean(costs):.4f}, total ${sum(costs):.2f}'
            if costs else '- Coste: el CLI no lo devolvio']
    path = EVAL_DIR / f'results_{args.version}.md'
    path.write_text('\n'.join(out) + '\n', encoding='utf-8')
    print('\n'.join(out[-4:]))
    print(f'-> {path}')
    return 0 if gate_answered and gate_null else 1


if __name__ == '__main__':
    sys.exit(main())
