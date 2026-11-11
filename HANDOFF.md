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
- [x] **ITEM 3 — Juiz que checa fatos (`news_auto.py` + `settings.json`)**: feito
  - **Commit:** `38a5e39cc632f5aad2c3342679f09473333cd35f` — *"Adiciona juiz de verificacao de fatos, fallback para juiz fraco e novos criterios de nota"*
  - **Arquivos tocados:** `news_auto.py`, `settings.json`, `README.md`, `CLAUDE.md`, `HANDOFF.md`, `tests/test_news_auto.py`
  - **Testes:** 51 testes passando (incluindo `test_item3_fact_checking_judge_and_criteria`)
- [x] **ITEM 4 — Passo 1 devolve o ângulo (`news_auto.py`)**: feito
  - **Commit:** `4643e641b4a2f70d325438b7ee70e7f0f8d60152` — *"Extrai angulo e contradicao no passo 1 e exige resposta para perguntas no roteiro"*
  - **Arquivos tocados:** `news_auto.py`, `CLAUDE.md`, `HANDOFF.md`, `tests/test_news_auto.py`
  - **Testes:** 52 testes passando (incluindo `test_item4_step1_hook_and_contradiction`)
- [x] **ITEM 5 — Limpar o corpo da matéria (`news_auto.py`, `article_text`)**: feito
  - **Commit:** a registrar após push — *"Limpa chamadas e manchetes de leia tambem do corpo da materia no RSS"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 53 testes passando (incluindo `test_item5_clean_article_body_html_and_flat_text`)
- [ ] **ITEM 6 — Legendas e acabamento (`captions.py`, `instalar_legendas.bat`, `README.md`, `engine.py`, testes)**: pendente / próximo

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
- **Status:** feito
- **O que foi feito:**
  - Implementada função `verificar_fatos(lines, facts, ask)`: para cada fala, o modelo devolve `{"falas":[{"i":0,"suportada":true,"motivo":""}]}`. Se qualquer fala vier com `"suportada": false`, o roteiro é regenerado recebendo correção com índice, texto da fala e motivo, contando dentro do limite de 2 regenerações.
  - Implementada função `call_judge` com fallback inteligente: se o modelo juiz responder HTTP 404 no Ollama, loga `"juiz padrão indisponível, usando o modelo do redator"`, faz fallback para o modelo do redator (`qwen3.5:4b`) e grava `{"juiz_fraco": true}` nos metadados do roteiro, reel e rejeições.
  - `"judge_model": "qwen3:8b"` configurado em `settings.json`, com documentação de `ollama pull qwen3:8b` no `README.md`.
  - Critérios do `evaluate_script()` atualizados: inclui checagem de "responde as perguntas feitas?" e "contradiz a matéria?". Falha em qualquer um limita a nota a no máximo **5**.
  - Testes cobrindo: fala não suportada forçando regeneração com correção; HTTP 404 ativando fallback e gravando `juiz_fraco`; e notas limitadas a 5 quando pergunta fica sem resposta ou contradiz a matéria.

### ITEM 4 — Passo 1 devolve o ângulo (`news_auto.py`)
- **Status:** feito
- **O que foi feito:**
  - `extract_facts()` devolve objeto `FactsResult` contendo `{"fatos": [5 a 6], "contradicao": "... ou null", "gancho": "pergunta de até 10 palavras baseada na contradição ou no fato mais surpreendente"}` e compatível com indexação/iteração de lista.
  - Função `validate_hook()`: valida se o gancho tem até 10 palavras, só usa números da matéria e não repete a manchete.
  - No Passo 2 (`write_script()`), se o gancho do Passo 1 for validado, ele é injetado como primeira fala obrigatória (`primeira_fala_obrigatoria_gancho` no payload e instrução explícita). Se for inválido, o redator escreve como antes.
  - Instrução no Passo 2 exige que perguntas feitas por um personagem sejam respondidas pelo outro usando os fatos da matéria.
  - Campos `"contradicao"` e `"gancho"` são salvos em `story`, no JSON de saída do reel e nos scripts rejeitados.

### ITEM 5 — Limpar o corpo da matéria (`news_auto.py`, `article_text`)
- **Status:** feito
- **O que foi feito:**
  - `_NewsHTMLCleaner` parseia HTML de `content:encoded` e `summary` com `html.parser.HTMLParser`.
  - Remove tags `<ul>/<ol>/<li>`, `<figure>`, `<figcaption>`, `<aside>`, `<script>`, `<style>` e elementos cuja classe ou id contenha `"related"`, `"relacionad"`, `"leia"`, `"veja"`, `"saiba"`.
  - Remove links `<a>` isolados (links que ocupam sozinhos o parágrafo ou bloco sem texto antes/depois).
  - Preserva links inline dentro de frases normais (`Segundo a <a href="...">entidade</a>...`).
  - Fallback para texto plano (`strip_glued_headlines`): remove sequências de 2 ou mais trechos curtos (6 a 20 palavras) sem pontuação final iniciados em maiúscula entre frases normais.
  - Testes passando (`test_item5_clean_article_body_html_and_flat_text`): verifica remoção completa de elementos HTML e valida a string de "leia também" colada do caso real do café (sem `"31%"`, sem `"melhor agosto da história"`, sem `"20 meses"` e sem `"20% em julho"`).
- **Próxima ação concreta:** Implementar ITEM 6 (`captions.py`, `instalar_legendas.bat`, `README.md`, `engine.py`, `tests/test_integration.py`).

### ITEM 6 — Legendas e acabamento
- **Status:** pendente
- **Escopo:**
  - `captions.match_words`: palavras interpoladas com menos de 0,08 s ganham pelo menos 0,08 s, tomando tempo das vizinhas.
  - `instalar_legendas.bat`: baixa modelo lido de `settings.json`. Em `whisper_words`, RuntimeError amigável se ausente.
  - `README.md`: sprites são `sasuke.png` e `naruto.png`.
  - `engine.character`: thumbnail volta para `(510, 640)`.
  - Testes com WAV demo geram WAV silencioso quando arquivo não existir.
- **Arquivos a tocar:** `captions.py`, `instalar_legendas.bat`, `README.md`, `engine.py`, `tests/test_integration.py`.
