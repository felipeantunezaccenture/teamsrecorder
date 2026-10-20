# Eval: meeting_qa (preguntas sobre el transcript de una reunión)

Definida el 19/10/2026, ANTES de escribir el componente (`/ecc-eval`, fase 1).

## Qué se evalúa

`meeting_qa.ask()`: responde a una pregunta del usuario a partir del transcript (automático, con
errores de reconocimiento) de una reunión hasta un minuto dado, citando `[mm:ss]`, y diciendo que no
se habló de algo cuando no se habló. Modelo por defecto: `claude-sonnet-5-5`, esfuerzo bajo, sin
herramientas (`claude -p --safe-mode --tools "" ...`).

Entrada: `reunion_sintetica.txt`, una reunión inventada de 9 minutos con hechos conocidos, en el
formato exacto de `live_transcriber.render` (`[mm:ss] [Felipe]: ...` / `[mm:ss] [Otros]: ...`), con
un error de reconocimiento a propósito ("mirro" en 03:30). Sin datos reales de nadie.

Casos: `cases.json` (14). Corredor: `scripts/eval_meeting_qa.py`. Cada caso, 3 veces.

## Clases

| Clase | Casos | Qué mide |
|---|---|---|
| factual | F1-F5 | un dato dicho (fecha, cifra, sitio, riesgo) |
| decision | D1-D3 | qué se decidió, quién hace qué, qué quedó abierto |
| temporal | T1-T2 | "al principio", "los últimos dos minutos" |
| language | E1 | pregunta en inglés: responde en inglés |
| null | N1-N3 | algo de lo que NO se habló. N3 tienta a inventar una cifra |

## Graders (todos de código, deterministas)

Para cada respuesta:
1. **Minutos válidos (todas las clases):** todo `[mm:ss]` citado existe en el transcript, o es el
   `upto` (09:12). Un minuto inventado es FAIL en cualquier clase.
2. **factual / decision / temporal / language:** cada grupo de `groups` tiene al menos uno de sus
   términos en la respuesta (sin mayúsculas ni acentos); cita al menos un minuto, y alguno está a
   ≤ 20 s de uno de `ts`. En `language`, además, más palabras vacías inglesas que españolas.
3. **null:** contiene "no se ha hablado / no se habló / no se ha mencionado / no se mencionó / no se
   ha dicho / no se dijo". Con `no_numbers`, ninguna cifra fuera de los `[mm:ss]`.

## Umbrales (fijados antes de medir; cambiarlos después de ver resultados es trampa)

- **Con respuesta (F, D, T, E: 11 casos):** un caso pasa si pasa en al menos 2 de 3 ejecuciones.
  Hacen falta **≥ 10 de 11** (≈ 9 de 10 del plan). Se informa también pass@1.
- **null (N: 3 casos):** **las 9 ejecuciones** tienen que pasar. Inventar es peor que no saber.
- **Latencia (objetivo, se informa):** primer texto p50 ≤ 8 s; respuesta completa p50 ≤ 15 s. Si no
  se cumple, se prueba Haiku 5.5 antes de dar por buena la elección del modelo.
- **Coste:** se informa por pregunta (`total_cost_usd` del CLI).

Si no se llega a los umbrales de calidad, no se integra.

## Resultados

En `results_v<N>.md`: solo PASS/FAIL, latencias y coste. Nunca el texto de las respuestas.

- v1 (llamada suelta): calidad OK, latencia fuera de objetivo (14,9 / 17,9 s): era el arranque del CLI.
- v2 (sesión abierta + prompt "[mm:ss] solo para citar líneas"): 11/11 + 9/9, 1,9 / 4,0 s, $0,008.
- Haiku 5.5 descartado (QA del 19/10/2026): con esfuerzo bajo piensa 8.500-12.500 tokens por
  pregunta, 41-49 s y $0,10-0,17. No es la opción rápida; ya no se ofrece.
