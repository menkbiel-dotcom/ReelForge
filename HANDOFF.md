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
  - **Commit:** `4fb4bdc1f42c2ad40a04bfef41118276bcb71c01` — *"Faz checagem de fatos falhar fechada quando o juiz estiver indisponivel"*
  - **Arquivos tocados:** `news_auto.py`, `settings.json`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 62 testes passando (incluindo `test_item6_round2_fact_check_fails_closed`)
- [x] **ITEM 7 — Prompt do redator e transparência (`news_auto.py`, `engine.py`)**: feito
  - **Commit:** `14f05174c45688d08234fa8b7fa5f12f62d52c32` — *"Atualiza prompt few-shot do redator e registra modelos e fatos no JSON final"*
  - **Arquivos tocados:** `news_auto.py`, `engine.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 63 testes passando (incluindo `test_item7_round2_writer_prompt_and_transparency`)
- [x] **ITEM 8 — A tela não pode apagar configurações (`gui.py`, `_save`)**: feito
  - **Commit:** `5260986cfcaa87bb8f3aa2f40b101756a407059b` — *"Preserva configuracoes desconhecidas ao salvar ajustes pela interface grafica"*
  - **Arquivos tocados:** `gui.py`, `tests/test_integration.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 65 testes passando (incluindo `test_merge_settings_preserves_unknown_keys_and_other_providers` e `test_gui_save_preserves_unknown_settings_on_disk`)
- [x] **ITEM 9 — Campos de modelo na tela (`gui.py`)**: feito
  - **Commit:** `92e989942cc55bd9abd060a540a916113b815f69` — *"Adiciona selecao de modelos Ollama e aceite de nota baixa na interface grafica"*
  - **Arquivos tocados:** `gui.py`, `news_auto.py`, `tests/test_integration.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 68 testes passando (incluindo `test_list_ollama_models_fallback_when_offline`, `test_list_ollama_models_success_from_api` e `test_gui_model_fields_and_accept_low_score_persistence`)

---

# Rodada 3 — Otimização de Performance, Resiliência de Feeds RSS e Fixtures

- [x] **Fixtures de testes (`tests/fixtures/rejected_*.json`)**: feito
  - **Commit:** `39ebffcae080a5e8d4d0c4545d1381976bac4887` — *"Adiciona fixtures de roteiros rejeitados usadas nos testes"*
  - **Arquivos tocados:** `tests/fixtures/rejected_20261111_181036_244323.json`, `tests/fixtures/rejected_20261111_192441_627729.json`
- [x] **Substituição de `strip_glued_headlines` por programação dinâmica (`news_auto.py`)**: feito
  - **Commit:** `c222866c2b9bc0cfacab2d211fc665b4eca3adab` — *"Substitui strip_glued_headlines por programacao dinamica para evitar tempo exponencial"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 70 testes passando (incluindo `test_strip_glued_headlines_dp_performance_and_clean` e `test_strip_glued_headlines_random_inputs_match_legacy`)
- [x] **Reforço em `gather_candidates` (limite 60s, shutdown sem espera e logs de feed)**: feito
  - **Commit:** `e42745eabbd0673b735687ba29b3bf5debbad61f` — *"Aplica limite de 60s, shutdown sem espera e logs por feed na leitura de RSS"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 72 testes passando (incluindo `test_gather_candidates_feed_logging` e `test_gather_candidates_timeout_ignores_slow_feeds_and_shuts_down`)

---

# Rodada 4 — Resiliência na Geração de Roteiros (Itens 10 a 13)

- [x] **ITEM 10 — Relink automático do índice do fato (`news_auto.py`)**: feito
  - **Commit:** `51a9b024584ce5cadfdb224a09d8d237dc94aae8` — *"Relinca automaticamente indice do fato quando numeros e entidades pertencem a outro fato"*
  - **Arquivos tocados:** `news_auto.py`, `settings.json`, `tests/test_news_auto.py`, `tests/test_integration.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 73 testes passando (incluindo `test_item10_relink_automatic_fact_index`)
- [x] **ITEM 11 — Feedback útil entre tentativas (`news_auto.py`)**: feito
  - **Commit:** `873daca2caf2ad354df057af76a2ad5b21a0ac60` — *"Melhora feedback de correcao entre tentativas e loga status de cada tentativa"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 74 testes passando (incluindo `test_item11_feedback_useful_between_retries_and_attempt_logging`)
- [x] **ITEM 12 — Registro de falha (`news_auto.py`)**: feito
  - **Commit:** `5aa3f05032eff8cae4975bd6377a25bfd4ebb853` — *"Registra diagnostico completo de falha em JSON quando esgotar 8 tentativas"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 75 testes passando (incluindo `test_item12_failure_log_file_and_runtime_error`)
- [x] **ITEM 13 — Tentar outra pauta antes de desistir (`news_auto.py`)**: feito
  - **Commit:** `f641357d6c5363d8bae9f25a10ee7394d8d4bd16` — *"Tenta pautas candidatas alternativas quando a geracao do roteiro esgotar tentativas"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 76 testes passando (incluindo `test_item13_automatic_retries_next_candidate_on_script_failure`)

---

# Rodada 5 — Reparo Determinístico, Retries Inteligentes e Robustez de Geração (Itens 14 a 19)

- [x] **ITEM 14 — Reparo determinístico de reações longas (`news_auto.py`)**: feito
  - **Commit:** `991668a4e640ae98741198c6c86f9c4f20916c50` — *"Repara deterministicamente falas de reacao longas ou com digitos antes da validacao"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 77 testes passando (incluindo `test_item14_deterministic_repair_long_reactions`)
- [x] **ITEM 15 — Retries com diversidade e correção concreta (`engine.py`, `news_auto.py`)**: feito
  - **Commit:** 4ff6d7cb70837e90d293a0af268abbd0b0f20dee — *"Adiciona diversidade de temperatura, exemplos concretos na correcao e abandono antecipado"*
  - **Arquivos tocados:** `engine.py`, `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 78 testes passando (incluindo `test_item15_retries_diversity_concrete_correction_and_early_abort`)

### Detalhes do ITEM 15
- `engine.request_json` aceita `temperature` (padrão 0.55) e repassa para as opções do Ollama.
- `write_script` calcula temperatura escalonada por tentativa: tentativas 1 e 2 = 0.55; tentativas 3 a 8 sobem +0.1 por tentativa até 0.9.
- `_add_concrete_example(error_msg)` anexa exemplos concretos e curtos em `payload['correcao']` para gancho, reações, fecho, números ausentes, vocativos e concordância.
- Se o mesmo motivo curto de erro se repetir por 3 tentativas consecutivas, loga `erro repetido 3x: <motivo>`, grava diagnóstico de falha e lança `RuntimeError` para que `automatic()` tente a próxima pauta candidata.
- [x] **ITEM 16 — Registrar a causa real de "checagem de fatos indisponível" (`news_auto.py`)**: feito
  - **Commit:** 989f2bae272c7820e1d11f6a2a5e485fc9b39742 — *"Registra causa concreta de indisponibilidade da checagem de fatos e deduplica rejeicoes"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 79 testes passando (incluindo `test_item16_fact_check_failure_reasons_and_single_rejected_file`)

### Detalhes do ITEM 16
- `_extract_fact_check_failure_reason` captura a causa real da falha: status HTTP (ex.: `404: modelo 'qwen3:8b' não instalado`), timeout (`timeout na chamada ao Ollama`), JSON inválido (primeiros 300 caracteres) ou ausência da chave 'falas'.
- `verificar_fatos` devolve `{"indisponivel": True, "motivo": motivo_concreto}` e loga a causa concreta no console.
- Em caso de HTTP 404, faz fallback UMA VEZ para o modelo redator como juiz e marca `juiz_fraco=true`.
- Investigação dos 3 arquivos `rejected_*.json` salvos com ~3ms de diferença: ocorria porque `_save_rejected_script` era chamado a cada tentativa do loop quando `vf.get('indisponivel')` acontecia, antes de verificar `quality_retries >= 2`. Corrigido para salvar apenas após esgotar as 2 regenerações, e adicionado reuso/sobrescrita por `story['_rejected_path']` garantindo que apenas 1 arquivo de rejeição seja salvo por pauta.
- Garantido que `source`, `modelos` e `motivo_checagem` estejam sempre presentes em `rejected_*.json`.
- [x] **ITEM 17 — Tamanho mínimo e fecho do roteiro (`news_auto.py`)**: feito
  - **Commit:** `f4ad7010ae9b829da60738d2b48f47084b8be06b` — *"Exige tamanho minimo de 8 falas, media de palavras e fecho com opiniao"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 80 testes passando (incluindo `test_item17_minimum_size_and_script_ending`)

### Detalhes do ITEM 17
- `validate_lines`:
  - `min_lines=8` por padrão (exige entre 8 e 12 falas em "lines" para reels de ≈ 25 a 40 s).
  - Média mínima de 6 palavras por fala calculada e exigida antes do juiz (`avg_words = total / len(texts) >= 6.0` quando `min_lines >= 8`).
  - Fecho / provocação na última fala:
    - Rejeita fechos iniciados por `"Será que ...?"` genérico sem termos de opinião/julgamento (`'acho', 'duvido', 'certeza', 'loucura', 'exagero', 'vale', 'compensa', 'justifica', 'arriscado', 'furada', 'besteira', 'revolucao', 'revolução', 'futuro', 'tendencia', 'tendência', 'perigo', 'bizarro', 'absurdo', 'sensato', 'burrice', 'piada', 'ilusao', 'ilusão', 'coragem', 'otimismo', 'pessimismo'`).
    - Rejeita fecho que repete dados já ditos no roteiro ($\ge 3$ palavras de conteúdo compartilhadas com qualquer fala anterior).
  - `_add_concrete_example`: atualizado para fecho com: `Exemplo de fecho válido com opinião: "Pra mim, investir bilhões nisso sem testar antes é loucura total." (nunca use "Será que ...?" genérico e não repita fatos já ditos).`
- [x] **ITEM 18 — Robustez do Passo 1 (`news_auto.py`)**: feito
  - **Commit:** `0da75814b33eb68ccd90ada2ddd7a0b22e139982` — *"Adiciona retry especifico para contradicao e gancho no passo 1 e fallback gracioso"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 81 testes passando (incluindo `test_item18_step1_hook_retry_and_graceful_fallback`)

### Detalhes do ITEM 18
- `extract_facts`:
  - Se `contradicao` ou `valid_hook` vierem nulos/vazios na resposta inicial do Passo 1, realiza 1 retry específico (`Passo 1B — Identificação de ângulo`) pedindo exclusivamente `{"contradicao": "...", "gancho": "..."}` a partir dos fatos já extraídos (`cleaned[:6]`), sem reextrair a lista de fatos.
  - Se após o retry a contradição ou gancho continuarem nulos, vazios ou inválidos (gancho reprovado por `validate_hook`), segue sem falhar a pauta, logando `"sem contradição/gancho"` e retornando o `FactsResult` sem gancho forçado.
  - Mantidas todas as validações de números permitidos (`_allowed_numbers`), unidades temporais (`TEMPORAL_UNITS`) e tamanho do gancho (`validate_hook`).
  - Atualizadas fixtures mock em testes legados (`test_low_score...`, `test_fictional_few_shot...`, `test_automatic_retries...`) para fornecer contradição/gancho em seus mocks de fatos.
- [x] **ITEM 19 — Estilo das falas (`news_auto.py`)**: feito
  - **Commit:** `1c73b7c03b7d2f48d50ff81c200e4767d8e156a0` — *"Reforca estilo falado e informal no prompt do redator com novo exemplo ficticio"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 82 testes passando (incluindo `test_item19_writer_prompt_reinforcement_and_fictional_examples`)

### Detalhes do ITEM 19
- `few_shot`:
  - Adicionado Exemplo 3 completo com 8 falas alternadas entre Sasuke e Naruto sobre assunto fictício absurdo (Kryptonix em Zyphoria com 412 mecazoids): gancho curto de Sasuke (<= 10 palavras), Naruto exagerando, Sasuke irônico com dados e qualificadores de arredondamento ("mais de 400", "cerca de 953"), reações curtas e fecho opinativo forte ("furada total").
- `instruction`:
  - Reforçado estilo falado e informal: instrução explícita para evitar tom de matéria lida ou telejornal (exemplos no prompt: em vez de "O Brasil exportou 34,478 milhões de sacas", usar "O Brasil mandou mais de 34 milhões de sacas pra fora"; em vez de "arrecadou US$ 1,786 bilhão", usar "faturou quase 1,8 bilhão de dólares").
  - Instrução explícita para arredondamento com qualificadores conforme `_quantity_pairs` ("mais de X", "quase X", "cerca de X").
  - Instrução explícita para uso moderado de vocativos (máx. 3 no roteiro todo, nunca em falas com números).
- `FICTIONAL_EXAMPLE_TERMS`:
  - Adicionados termos distintivos do Exemplo 3 (`'kryptonix'`, `'zyphoria'`, `'mecazoids'`, `'mecazoid'`, `'412'`, `'953'`) para garantir que sejam barrados por `validate_lines` caso vazem no roteiro sem constar na matéria real.
- Testes:
  - Adicionado `test_item19_writer_prompt_reinforcement_and_fictional_examples` em `tests/test_news_auto.py` validando todos os 15 termos em `FICTIONAL_EXAMPLE_TERMS`, rejeição de vazamentos do Exemplo 3 e inspeção do prompt gerado por `write_script`.

---

# Rodada 6 — Isolamento de Testes, Escolha Determinística de Pauta e Diagnóstico Ollama (Itens 20 a 23)

- [x] **ITEM 20 — Isolar testes do `output/` real (`tests/_isolation.py`, `tests/test_news_auto.py`, `tests/test_integration.py`)**: feito
  - **Commit:** `528e616dfa60482ec0094359ea014bca02b04720` — *"Isola engine.OUTPUT em diretorio temporario em todos os testes com guarda contra escrita real"*
  - **Arquivos tocados:** `tests/_isolation.py`, `tests/test_news_auto.py`, `tests/test_integration.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 83 testes passando em duas execuções consecutivas sem alterar `output/` real (`test_item20_isolated_output_guard`)

### Detalhes do ITEM 20
- Criado `tests/_isolation.py` com `IsolatedOutputMixin`, fazendo `patch.object(engine, 'OUTPUT', tempdir)` no `setUp()` com limpeza automática via `addCleanup`.
- Todas as classes `unittest.TestCase` de `tests/test_news_auto.py` (`NewsAutoTests`) e `tests/test_integration.py` (`SpeechTests`, `CaptionTests`, `AppTests`, `Item6Tests`, `Item8Tests`, `Item9Tests`) agora herdam `IsolatedOutputMixin`.
- Verificado que `_save_rejected_script` e `_save_failure_log` em `news_auto.py` leem `engine.OUTPUT` dinamicamente em tempo de chamada.
- Adicionado `test_item20_isolated_output_guard` que valida a herança do mixin em todas as classes, resolução dinâmica de `engine.OUTPUT` e executa toda a suíte em subprocesso confirmando `after - before == []` na pasta `output/` real.
- **Próximo item:** ITEM 21 (`news_auto.py`, `pick_story` determinístico).

- [x] **ITEM 21 — Escolha de pauta determinística por padrão (`news_auto.py`)**: feito
  - **Commit:** `647542b93a86324011882abb9c33acfc3e9749b3` — *"Torna pick_story deterministico por padrao com fallback rapido no modo LLM"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 84 testes passando (incluindo `test_item21_deterministic_pick_story_and_llm_fallback`)

### Detalhes do ITEM 21
- `pick_story`:
  - Fluxo padrão não chama LLM: filtra pautas ainda não usadas na tabela `used` (`_filter_unused_candidates`, respeitando `allow_repeat`), prioriza as que passam em `require_minimum_source()` e escolhe a de maior `complexity_score` (`_candidate_rank_score`).
  - Se `settings.json` definir `"escolha_pauta_llm": true`, envia ao LLM apenas `index`, `titulo` (máx. 140 chars) e `frase` (1ª frase com máx. 180 chars) de no máximo 12 pautas ranqueadas (< 1500 tokens). Em qualquer falha/timeout, loga `"escolha da pauta por LLM falhou; usando ranking"` e faz fallback imediato para `ranked[0]`.
  - Atualizado `automatic()` para repassar `allow_repeat` e `log` para `pick_story`.
- **Próximo item:** ITEM 22 (`engine.py`, timeouts e erros do Ollama amigáveis).

- [x] **ITEM 22 — Timeouts e erros do Ollama amigáveis (`engine.py`, `gui.py`)**: feito
  - **Commit:** `c0b515e1b023e486b6179bcb9ce02b9ffb542ee9` — *"Trata timeouts/erros do Ollama com log em erros.log, think=false e estimativa de tokens"*
  - **Arquivos tocados:** `engine.py`, `gui.py`, `tests/test_news_auto.py`, `tests/test_integration.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 85 testes passando (incluindo `test_item22_ollama_timeout_think_and_logging`)

### Detalhes do ITEM 22
- `engine.py`:
  - `ollama_timeout_seconds(cfg)`: lê `"ollama_timeout_s"` de `settings.json` ou `OLLAMA_TIMEOUT_S` (padrão `300` s) e passa em `timeout=(5, read_timeout)`.
  - `request_json`: envia `"think": False` na raiz do payload `/api/chat`; remove `"thinking"` da resposta/mensagem e limpa blocos `<think>...</think>` (`_strip_thinking_block`) antes de `json.loads`. Loga `"Ollama: modelo X, N tokens de prompt estimados"` (`(len(instruction) + len(payload)) / 3.5`) no início e `"Ollama: modelo X respondeu em Y.Y s"` ao terminar.
  - Em `requests.exceptions.ReadTimeout` / `ConnectionError`, grava o traceback completo em `output/erros.log` via `log_error_traceback` e lança `RuntimeError` em português sem traceback cru, orientando checar `'ollama ps'`, se o Ollama está aberto, modelo menor ou `num_ctx`.
- `gui.py`:
  - `_job`: exceções da thread worker gravam o traceback completo em `engine.log_error_traceback` e enviam apenas `('error', f'Erro: {exc}')` para a UI.
  - `App.destroy`: cancela `self._poll_id` e limpa `tk.Variable` na thread principal antes de destruir a janela; `tests/test_integration.py` força `gc.collect()` no `finally` dos testes GUI para evitar `Tcl_AsyncDelete` quando rodam junto com `test_news_auto`.
- **Próximo item:** ITEM 23 — Diagnóstico no "Verificar ambiente" (`gui.py` / `news_auto.py`).

- [x] **ITEM 23 — Diagnóstico no "Verificar ambiente" (`gui.py`)**: feito
  - **Commit:** `353dd861cbe421c82af1050f83e89d5aeb7da101` — *"Adiciona diagnostico completo do Ollama (tags, ps/VRAM e teste de latencia) ao botao Verificar ambiente"*
  - **Arquivos tocados:** `gui.py`, `tests/test_integration.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 87 testes passando (incluindo `test_diagnose_ollama_environment_reports_ps_probe_and_missing_models` e `test_gui_check_button_runs_ollama_diagnostic`)

### Detalhes do ITEM 23
- `gui.py`:
  - `diagnose_ollama_environment(writer_model, judge_model='', base_url=None, probe_timeout=30.0)`:
    - Consulta `GET /api/tags` verificando se redator e juiz estão presentes (`_is_model_installed`); se algum faltar, avisa em português com `ollama pull <modelo>`.
    - Executa chamada de teste `"responda só: ok"` via `POST /api/chat` (`think: false`, `stream: false`) tanto para o redator quanto para o juiz e reporta o tempo decorrido de cada um.
    - Consulta `GET /api/ps` listando modelos carregados com `size_vram` vs `size` (`_format_bytes`) e avisando em português caso `size_vram < size` que parte está na CPU e a geração será lenta.
  - `App._check`: captura `ollama_model`, `judge_model` e `provider` na thread principal e registra as linhas de `diagnose_ollama_environment` no console da GUI.
- **Estado atual:** Itens da Rodada 6 (20 a 23) concluídos; iniciada Rodada 7 (itens 25 a 30 + regressão com falhas reais).

---

## Rodada 7 (Itens 25 a 30 + Regressão Real)

- [x] **ITEM 25 — Inferir fato/reação no código (`news_auto.py`)**: feito
  - **Commit:** `f664a2e8f9714b9323808347678de976fe46cdd4` — *"Infere classificacao fato/reacao/opiniao no codigo e valida contra a uniao da materia"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 88 testes passando (incluindo `test_item25_infer_facts_reactions_and_opinions`)

### Detalhes do ITEM 25
- `write_script`: o prompt agora pede ao modelo apenas `{"lines":[{"speaker":0,"text":"..."}]}` (removidos os campos `"fato"` e `"reacao"` das instruções e dos exemplos *few-shot*; mantidos os `fatos_da_materia` numerados no payload).
- `annotate_lines(lines, facts, title='', log=print)`: classifica cada fala deterministicamente em `fato` ($\ge 2$ palavras de conteúdo ou $\ge 1$ número em comum com algum fato, registrando `matched_facts` para permitir combinação de fatos), `reacao` (até 6 palavras e nenhum dígito), ou `opiniao` (sem dígitos, sem entidades ausentes de fatos+título, máx. 14 palavras, máx. 3 por roteiro), logando `fala X: inferida como fato N` / `reação` / `opinião`.
- `validate_lines`: valida números, quantidades (`_quantity_pairs`), entidades (`_missing_entities`) e `TEMPORAL_UNITS` contra a **união** de fatos + título + corpo da matéria (sem rejeições artificiais por índice de fato trocado ou fusão de fatos); exige pelo menos 4 fatos distintos usados (em 6+ falas) e verifica gancho respondido pela combinação das falas 2–3 (`texts[1:3]`).

- [x] **ITEM 26 — Normalizar/reparar antes de rejeitar (`news_auto.py`)**: feito
  - **Commit:** `34df47c2372bc0c68e57a00b798ab359d3caecc1` — *"Normaliza e repara falas antes de validar (faixa 6-12 e media nao-reacao)"*
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 89 testes passando (incluindo `test_item26_normalize_repair_and_line_bounds`)

### Detalhes do ITEM 26
- `normalize_lines(raw_or_lines, facts=None, title='', log=print, max_lines=12)`: converte `speaker` string (`"0"`/`"1"`) para `int`, descarta chaves estranhas deixando só `speaker`/`text`, remove reações no fim repetidamente, remove reações excedentes quando $> 3$ (mantendo até 2), une falas consecutivas do mesmo `speaker` quando $\le 22$ palavras (rejeitando se $> 22$) e remove reações/opiniões do meio quando $> 12$ falas, logando cada reparo (`"reparo: ..."`).
- `turn_range()` e `validate_lines()` ajustados para faixa **6 a 12 falas**; média de palavras calculada exclusivamente sobre falas não-reação (rejeita se `< 5.0` e avisa entre `5.0` e `6.0`).

- [x] **ITEM 27 — Política de retries (`news_auto.py`)**: feito
  - **Arquivos tocados:** `news_auto.py`, `tests/test_news_auto.py`, `CLAUDE.md`, `HANDOFF.md`
  - **Testes:** 90 testes passando (incluindo `test_item27_retry_policy_cap_and_low_temp_on_malformed_json`)

### Detalhes do ITEM 27
- Temperatura em `write_script`: escala suavemente de `0.55` aumentando `+0.05` por tentativa até o teto de `0.7` (`min(0.7, round(0.55 + (attempt - 1) * 0.05, 2))`), nunca subindo até `0.9`.
- Se a resposta anterior vier com `JSONDecodeError` ou estrutura malformada (`_is_malformed_script_response`: falta `"lines"`, não é lista, itens não-objeto ou falta `"speaker"`/`"text"`), força temperatura `0.4` na tentativa seguinte e inclui o esquema JSON exato (`_EXACT_SCRIPT_SCHEMA`) na correção.
- Mantido o aborto antecipado após 3 repetições consecutivas do mesmo erro curto.
- **Próximo item:** ITEM 28 — Guardas de qualidade textual (`news_auto.py`).
