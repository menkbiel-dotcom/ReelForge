# HANDOFF.md — Estado de Continuidade do ReelForge

## Status Geral dos Itens

- [x] **ITEM 1 — Cada fala amarrada a um fato (`news_auto.py`)**: feito
  - **Commit:** `b00a6dfb2eb51fb57a36d9aad1cb06e1228d1611` — *"Amarra cada fala a um fato ou reacao curta e proibe fecho de enchimento"*
  - **Arquivos tocados:** `news_auto.py`, `engine.py`, `tests/test_news_auto.py`
  - **Testes:** 48 testes passando (incluindo `test_item1_tied_to_fact_and_reaction_rules`)
- [x] **ITEM 2 — Validação de quantidades e unidades (`news_auto.py`)**: feito
  - **Commit:** `7b0d04a4224fc1275a526b348b5a875bf372f2f9` — *"Valida quantidades e magnitudes com qualificadores e restringe unidades temporais"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 50 testes passando (incluindo `test_item2_quantity_pairs_and_qualifier_rules` e `test_item2_temporal_units_restriction`)
- [ ] **ITEM 3 — Juiz que checa fatos (`news_auto.py` + `settings.json`)**: em andamento / próximo
- [ ] **ITEM 4 — Passo 1 devolve o ângulo (`news_auto.py`)**: pendente
- [ ] **ITEM 5 — Limpar o corpo da matéria (`news_auto.py`, `article_text`)**: pendente
- [ ] **ITEM 6 — Legendas e acabamento (`captions.py`, `instalar_legendas.bat`, `README.md`, `engine.py`, testes)**: pendente

---

## Detalhes de Cada Item

### ITEM 1 — Cada fala amarrada a um fato (`news_auto.py`)
- **Status:** feito
- **O que foi feito:**
  - Passo 2 do Ollama e `validate_lines` agora exigem que cada fala tenha `{"fato": N}` (N = 0..5) ou `{"reacao": true}`.
  - Falas de reação: máximo 6 palavras, sem dígitos, máximo 2 no roteiro todo, nunca consecutivas e nunca na última fala.
  - Falas com `fato: N`: números e nomes próprios precisam constar no fato N ou no título da matéria.
  - Proibidos fechos de enchimento na última fala com regex `FILLER_CLOSE` ("que legal", "que bom", "incrível", "uau", "show", "demais").
  - `engine.shorten_script` preserva os campos `fato` e `reacao`.

### ITEM 2 — Validação de quantidades e unidades (`news_auto.py`)
- **Status:** feito
- **O que foi feito:**
  - Criado `_quantity_pairs(text, include_qualifiers=False)`: extrai pares (valor numérico float, magnitude normalizada) com classe `Quantity` e `NormalizedMagnitude` para magnitudes em `{mil, milhão/milhões, bilhão/bilhões, trilhão/trilhões, %, sacas, quilos, toneladas...}`.
  - Converte números por extenso via `_parse_extenso` ("dezesseis" = 16, "um" só conta quando seguido de mil/milhão/bilhão/trilhão) e decimais brasileiros ("1,786" = 1.786).
  - Regra de validação com qualificadores em `_match_quantity_rule`:
    - Sem qualificador: valor exato (`abs(source - line) < 0.001`).
    - `"mais de X"`: aceita se `math.floor(valor da fonte) == X`.
    - `"quase X"`: aceita se `math.ceil(valor da fonte) == X`.
    - `"cerca de X"`: aceita se `round(valor da fonte) == X`.
  - Casos obrigatórios cobertos nos testes unitários:
    - `"quase um bilhão"` com fonte `1,657 bilhão` -> REJEITA (`ceil(1.657) == 2 != 1`).
    - `"mais de dezesseis milhões"` com fonte `16,591 milhões` -> ACEITA (`floor(16.591) == 16`).
    - `"US$ 1,786 bilhão"` com fonte `1,786 bilhão` -> ACEITA.
  - Removidos `"um, uma, primeiro, primeira, segundo, segunda, terceiro, terceira"` de `WORD_TO_NUM`.
  - Unidades temporais (`TEMPORAL_UNITS`: `trimestre`, `semestre`, `bimestre`, `quadrimestre`, `década`, `século`, `quinzena`): checadas sem acento via `_normalize_plain`; se ausentes da fonte, rejeitadas em `extract_facts` (com retry) e em `validate_lines`. Teste: `"primeiro trimestre"` com fonte `"quatro primeiros meses"` -> REJEITA.
  - `_facts()` agora também computa quantidades reconhecidas por `_quantity_pairs`.

### ITEM 3 — Juiz que checa fatos (`news_auto.py` + `settings.json`)
- **Status:** pendente
- **Escopo:**
  - Nova função `verificar_fatos(lines, facts, ask)`: para cada fala, o modelo devolve `{"falas":[{"i":0,"suportada":true,"motivo":""}]}`. Qualquer `"suportada": false` regenera o roteiro passando o índice, o texto da fala e o motivo como correção. Conta dentro do limite atual de regenerações.
  - Incluir no avaliador de nota o critério "responde as perguntas feitas?" e "contradiz a matéria?". Falha limita a nota a 5.
  - `judge_model` em `settings.json` vira `"qwen3:8b"`. Se HTTP 404 no Ollama, logar "juiz padrão indisponível, usando o modelo do redator" e gravar `"juiz_fraco": true` no JSON de saída. Documentar no README: `ollama pull qwen3:8b`.
- **Arquivos a tocar:** `news_auto.py`, `settings.json`, `README.md`, `tests/test_news_auto.py`.

### ITEM 4 — Passo 1 devolve o ângulo (`news_auto.py`)
- **Status:** pendente
- **Escopo:**
  - `extract_facts` devolve `{"fatos":[5 a 6], "contradicao":"... ou null", "gancho":"pergunta de até 10 palavras baseada na contradição ou no fato mais surpreendente"}`.
  - Gancho validado e passado ao redator como primeira fala obrigatória.
  - Gravar `"contradicao"` e `"gancho"` no JSON de saída.
- **Arquivos a tocar:** `news_auto.py`, `tests/test_news_auto.py`.

### ITEM 5 — Limpar o corpo da matéria (`news_auto.py`, `article_text`)
- **Status:** pendente
- **Escopo:**
  - Parsear HTML de `content:encoded` e remover `<a>` isolados, `<ul>/<ol>/<li>`, `<figure>`, `<aside>` e classes com `"related"`, `"leia"`, `"veja"`, `"saiba"`.
  - Fallback texto plano: remover sequências de 2 ou mais trechos curtos sem pontuação final iniciados em maiúscula.
  - Teste com string de café: resultado não pode conter `"31%"` nem `"melhor agosto da história"`.
- **Arquivos a tocar:** `news_auto.py`, `tests/test_news_auto.py`.

### ITEM 6 — Legendas e acabamento
- **Status:** pendente
- **Escopo:**
  - `captions.match_words`: palavras interpoladas com menos de 0,08 s ganham pelo menos 0,08 s, tomando tempo das vizinhas.
  - `instalar_legendas.bat`: baixa modelo lido de `settings.json`. Em `whisper_words`, RuntimeError amigável se ausente.
  - `README.md`: sprites são `sasuke.png` e `naruto.png`.
  - `engine.character`: thumbnail volta para `(510, 640)`.
  - Testes com WAV demo geram WAV silencioso quando arquivo não existir.
- **Arquivos a tocar:** `captions.py`, `instalar_legendas.bat`, `README.md`, `engine.py`, `tests/test_integration.py`.
