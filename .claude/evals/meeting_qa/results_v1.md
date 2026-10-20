# meeting_qa v1 - claude-sonnet-5-5, 3 ejecuciones por caso

| Caso | Clase | Ej. 1 | Ej. 2 | Ej. 3 | Pasa | Motivo |
|---|---|---|---|---|---|---|
| F1 | factual | PASS | PASS | PASS | PASS |  |
| F2 | factual | PASS | PASS | PASS | PASS |  |
| F3 | factual | PASS | PASS | PASS | PASS |  |
| F4 | factual | PASS | PASS | PASS | PASS |  |
| F5 | factual | PASS | PASS | PASS | PASS |  |
| D1 | decision | PASS | PASS | PASS | PASS |  |
| D2 | decision | PASS | PASS | PASS | PASS |  |
| D3 | decision | PASS | PASS | PASS | PASS |  |
| T1 | temporal | PASS | PASS | PASS | PASS |  |
| T2 | temporal | FAIL | PASS | FAIL | FAIL | minuto inventado 07:12 |
| E1 | language | PASS | PASS | PASS | PASS |  |
| N1 | null | PASS | PASS | PASS | PASS |  |
| N2 | null | PASS | PASS | PASS | PASS |  |
| N3 | null | PASS | PASS | PASS | PASS |  |

- Con respuesta: 10/11 por mayoria (umbral >= 10); pass@1 91% -> OK
- No dichas: 9/9 ejecuciones (umbral todas) -> OK
- Primer texto p50 14.9 s (objetivo <= 8); total p50 17.9 s (objetivo <= 15)
- Coste por pregunta: media $0.0086, total $0.36
