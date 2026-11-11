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
  - **Commit:** `83f5e16176c873d7aefc866d9c3f2450da9ff065` — *"Limpa chamadas e manchetes de leia tambem do corpo da materia no RSS"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 53 testes passando (incluindo `test_item5_clean_article_body_html_and_flat_text`)
- [x] **ITEM 6 — Legendas e acabamento (`captions.py`, `instalar_legendas.bat`, `README.md`, `engine.py`, testes)**: feito
  - **Commit:** `cbb109ef19c9d6f09979a79b85ab416ad0f3e363` — *"Ajusta duracao minima de palavras nas legendas, modelo do whisper e documentacao"*
  - **Arquivos tocados:** `captions.py`, `instalar_legendas.bat`, `README.md`, `engine.py`, `tests/test_integration.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 56 testes passando (incluindo `test_captions_interpolated_words_expand_minimum_duration`, `test_whisper_words_missing_model_raises_runtime_error_without_hf_traceback` e `test_engine_character_thumbnail_size`)

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
- **Status:** feito
- **O que foi feito:**
  - `captions.expand_interpolated_words`: palavras interpoladas com duração menor que 0,08 s ganham pelo menos 0,08 s tomando tempo das vizinhas (à esquerda e depois à direita), mantendo estritamente a ordem cronológica e o fim da fala.
  - Teste com a fala `"Isso com o preço..."` onde `"Isso com"` tinha 0,0005 s: palavras ganham >= 0,08 s e preservam o fim da fala.
  - `captions.whisper_words`: se o modelo não estiver baixado localmente, levanta `RuntimeError` amigável em português informando o comando exato e `instalar_legendas.bat`, sem traceback do HuggingFace (`from None`).
  - `instalar_legendas.bat`: após o pip install, baixa o modelo lido de `settings.json` (`whisper_model`).
  - `README.md`: atualizado para documentar que os sprites são `sasuke.png` e `naruto.png`.
  - `engine.character`: thumbnail dos sprites restaurada para `(510, 640)`.
  - `tests/test_integration.py`: testes que dependem do áudio demo agora geram automaticamente um arquivo WAV silencioso usando o módulo padrão `wave` quando ausente no disco, permitindo a execução de `test_imported_audio_never_synthesized`.
- **Status final:** TODOS OS ITENS DE 1 A 6 CONCLUÍDOS COM SUCESSO.

---

# Rodada 2 — Correção Factual Avançada, Validações Semânticas, Juiz com Evidências e Ajustes de GUI

## Status Geral dos Itens (Rodada 2)

- [x] **ITEM 1 — Cobertura do tema (`news_auto.py`, `validate_lines`)**: feito
  - **Commit:** `4a1d9262e49fae3b4d210786c4c7574a23996418` — *"Exige cobertura das palavras-chave do titulo nos primeiros turnos e no roteiro"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `settings.json`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 57 testes passando (incluindo `test_item1_round2_theme_coverage`)
- [x] **ITEM 2 — Gancho respondido (`news_auto.py`, `validate_lines`)**: feito
  - **Commit:** `a1bc1d94f8e5f0766a536c476272cee4d811e738` — *"Exige que a pergunta do gancho seja respondida com fatos nos turnos seguintes"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 58 testes passando (incluindo `test_item2_round2_answered_hook_requirement`)
- [x] **ITEM 3 — Marcação "fato" não pode ser decorativa (`news_auto.py`, `validate_lines`)**: feito
  - **Commit:** `ab1c1534f258291a7bdffbe776c9bec5e4c9981b` — *"Exige sobreposicao real de conteudo entre a fala e o fato citado"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 59 testes passando (incluindo `test_item3_round2_non_decorative_fact`)
- [x] **ITEM 4 — Variedade e muletas (`news_auto.py`, `validate_lines`)**: feito
  - **Commit:** `d31fd82fcaba34337f71fbe936a32058435df0da` — *"Limita muletas de concordancia, vocativos e proporcao de perguntas no dialogo"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 60 testes passando (incluindo `test_item4_round2_variety_and_crutches`)
- [x] **ITEM 5 — Juiz com evidência (`news_auto.py`, `evaluate_script`)**: feito
  - **Commit:** `85b215887a6d0e8e564a218b39a1c44f21a074cd` — *"Exige problemas verificaveis com trecho da fala na avaliacao do juiz"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 61 testes passando (incluindo `test_item5_round2_judge_with_evidence`)
- [x] **ITEM 6 — Juiz de fatos não falha aberto (`news_auto.py`, `verificar_fatos`)**: feito
  - **Commit:** pendente de push — *"Faz checagem de fatos falhar fechada quando o juiz estiver indisponivel"*
  - **Arquivos tocados:** `news_auto.py`, `settings.json`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 62 testes passando (incluindo `test_item6_round2_fact_check_fails_closed`)
- [ ] **ITEM 7 — Prompt do redator e transparência (`news_auto.py`, `engine.py`)**: pendente
- [ ] **ITEM 8 — A tela não pode apagar configurações (`gui.py`, `_save`)**: pendente
- [ ] **ITEM 9 — Campos de modelo na tela (`gui.py`)**: pendente
