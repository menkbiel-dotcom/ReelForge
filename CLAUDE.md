# CLAUDE.md — Guia de Arquitetura do ReelForge

Este repositório contém o **ReelForge**, um gerador local para Windows de Reels verticais (`1080x1920`, `30 FPS` padrão ou `60 FPS`) onde **Sasuke** (`speaker = 0`) e **Naruto** (`speaker = 1`) conversam de forma natural e informal sobre notícias reais com gameplay de parkour ao fundo.

## Fluxo Principal (100% Local exceto leitura de RSS)

1. **Coleta RSS (`news_auto.py`):**
   - Lê feeds RSS de veículos brasileiros que trazem o texto completo da matéria (`content:encoded` de g1, CNN Brasil, Agência Brasil, Poder360, InfoMoney, Tecnoblog, Canaltech, Olhar Digital, ge) + Google News RSS.
   - `article_text()`: Limpa o corpo da matéria (`content:encoded` / `summary`), parseando HTML para remover tags `<ul>/<ol>/<li>`, `<figure>`, `<aside>`, elementos cuja classe/id contenha `related`, `relacionad`, `leia`, `veja`, `saiba`, e links `<a>` isolados. Em fallback para texto plano, remove sequências de 2 ou mais trechos curtos (6 a 20 palavras) sem pontuação final iniciados em maiúscula entre frases normais (`strip_glued_headlines`).
   - Em `parse_entry()`, itens do Google News (`feed_name=None`) **nunca** recebem `body` (seu resumo HTML é apenas lista de manchetes `<li>`), sendo barrados por `require_minimum_source()`.
   - Ranqueia as pautas por densidade informativa (`complexity_score`: números, entidades, tamanho do texto e frescor).
   - Em `automatic()`, se a pauta escolhida falhar em `require_minimum_source()`, o sistema tenta automaticamente a próxima candidata (até 3 tentativas) antes de falhar.
2. **Seleção e Roteiro em 2 Passos + Filtro de Qualidade Fechado (`news_auto.py` + Ollama local `qwen3.5:4b` e juiz `qwen3:8b`):**
   - `pick_story()`: O Ollama escolhe a melhor pauta com corpo completo.
   - **Passo 1 (`extract_facts()`):** O Ollama extrai de **5 a 6 fatos curtos**, identifica `"contradicao"` e gera `"gancho"` em JSON `{"fatos":[...],"contradicao":"...","gancho":"..."}` exclusivamente do título e corpo da matéria (`source_text`), validando números contra `_allowed_numbers(source)` e rejeitando unidades temporais inventadas (`TEMPORAL_UNITS`, com retry). Valida o gancho (`validate_hook`: até 10 palavras, sem números inventados e sem repetir a manchete) e grava `"contradicao"` e `"gancho"` no story e no JSON de saída do reel.
   - **Passo 2 (`write_script()`):** O Ollama escreve o diálogo entre Sasuke (direto e irônico) e Naruto (curioso e espontâneo) usando exemplos *few-shot* **totalmente fictícios** (`EXEMPLOS FICTÍCIOS: copie apenas o estilo e o ritmo, nunca os fatos`). Se o gancho do Passo 1 for validado, ele é injetado como primeira fala obrigatória. O prompt exige que perguntas feitas por um personagem sejam respondidas pelo outro usando um dos fatos.
   - **Validação (`validate_lines()`):**
     - Primeira fala deve ser uma provocação ou pergunta de até 10 palavras (nunca a manchete copiada nem iniciada por `"Mas..."`).
     - Cobertura do tema (`extract_title_keywords()`): extrai palavras-chave distintivas do título (normalizadas sem acento, sem stopwords e sem números); exige que pelo menos 1 apareça nos primeiros 3 turnos e que o roteiro cubra pelo menos 2 no total quando o título tiver 2 ou mais termos.
     - Gancho respondido: se a primeira fala for uma pergunta, a fala 1 ou 2 deve ser marcada "fato" (nunca "reacao") e seu fato correspondente deve compartilhar pelo menos 2 palavras de conteúdo ou 1 número com a pergunta, senão o roteiro é rejeitado.
     - Marcação "fato" não pode ser decorativa: uma fala com `fato: N` deve compartilhar pelo menos 2 palavras de conteúdo (sem stopwords, sem acento, minúsculas) ou 1 número com o fato N. Perguntas especulativas sem sobreposição são rejeitadas.
     - Variedade e muletas: no máximo 1 fala começando com muletas de concordância ("Exatamente", "Isso mesmo", "Verdade", "Concordo", "Pois é", "Realmente"); no máximo 3 vocativos (mano, cara, Sasuke, Naruto) no roteiro e nenhum em fala que carrega número; no máximo 50% das falas podem ser perguntas; e o Sasuke deve fazer pelo menos 1 pergunta ou provocação no diálogo.
     - Cada fala é amarrada a um fato (`{"fato": N}`) ou é uma reação curta (`{"reacao": true}`, máx. 6 palavras, sem dígitos, máx. 2 por roteiro, nunca consecutivas e nunca na última fala).
     - Proíbe citar o nome do veículo, expressões de telejornal (`"confirmado por"`, `"segundo o"`, `"nesta segunda-feira"`) e fechos de enchimento na última fala (`FILLER_CLOSE`: `"que legal"`, `"show"`, `"incrível"`).
     - Rejeita termos/números vazados dos exemplos fictícios do prompt (`FICTIONAL_EXAMPLE_TERMS`) quando não estiverem na matéria.
     - Valida números usando `_allowed_numbers(source)` (`WORD_TO_NUM` sem numerais ambíguos como `"um"`, `"primeiro"`, `"segundo"`), inteiros de decimais e algarismos romanos, calculado exclusivamente sobre título e corpo da matéria, nunca sobre manchetes relacionadas (`enrich()`).
     - Restringe unidades temporais (`TEMPORAL_UNITS`: `trimestre`, `semestre`, `década`, etc.): se ausentes da matéria, rejeita tanto em `extract_facts` (com retry) quanto em `validate_lines`.
     - Validação de quantidades e magnitudes com qualificadores (`_quantity_pairs`): compara pares numéricos e magnitudes (`mil`, `milhões`, `bilhões`, `trilhões`, `%`, `sacas...`), aceitando `"mais de X"` se `floor(fonte) == X`, `"quase X"` se `ceil(fonte) == X`, `"cerca de X"` se `round(fonte) == X`, ou valor exato sem qualificador.
     - Exige que a última fala termine com uma frase completa (pergunta aberta ou fecho), sem ponte truncada (`script_loop: false` por padrão).
   - **Checagem Factual e Filtro de Qualidade Fechado (`verificar_fatos()` + `evaluate_script()` + `deterministic_check()`):**
     - `verificar_fatos()`: o modelo juiz avalia se cada fala é suportada pelos fatos da matéria (`{"falas":[{"i":0,"suportada":true,"motivo":""}]}`). Falas não suportadas forçam regeneração do roteiro com correção contendo `{i, text, motivo}`, contando dentro do limite de 2 regenerações.
     - Permite configurar o modelo juiz via `"judge_model": "qwen3:8b"` em `settings.json`. Se o modelo retornar HTTP 404 no Ollama, loga `"juiz padrão indisponível, usando o modelo do redator"`, faz fallback para o redator (`qwen3.5:4b`) e grava `{"juiz_fraco": true}` nos metadados do roteiro e do Reel.
     - `evaluate_script()` (Juiz com evidência): remove critérios objetivos do prompt (gancho <= 10 palavras, pontuação final, veículo citado); foca exclusivamente em informalidade, ritmo e repetição. O juiz deve retornar `{"nota": N, "problemas": [{"i": idx, "trecho": "...", "tipo": "informalidade|ritmo|repeticao", "motivo": "..."}]}`. Problemas sem trecho exato na fala `i` ou com tipo inválido são descartados; se nota < 7 mas não restar problema verificável, descarta a reprovação, sobe nota para 7 e loga `'reprovação sem problema verificável; ignorada'`.
     - Se o Ollama falhar ou não retornar `"nota"` numérica válida, tenta mais 1 vez; se continuar falhando, retorna `{"nota": None, "problemas": ["avaliação indisponível"]}` e trata o roteiro como **NÃO aprovado** (nunca assume nota 8).
     - Executa `deterministic_check()` junto da nota do modelo (média de 6 a 16 palavras por fala, pelo menos uma pergunta com `?`, nenhuma fala repetida e nenhuma fala começando igual a duas anteriores); se falhar, desconta 2 pontos e adiciona os motivos em `"problemas"`.
     - Após 2 regenerações com `nota < 7` (ou `nota is None`), salva o roteiro reprovado em `output/rejected_<data>.json` com a nota e os problemas e levanta `RuntimeError` (a menos que `--aceitar-nota-baixa` na CLI ou `"aceitar_nota_baixa": true` em `settings.json` esteja ativo).
3. **Síntese de Voz (`speech.py` + `voice_studio.py`):**
   - Lê o provedor e os perfis de `settings.json` (`voicestudio` em `http://127.0.0.1:3900`, perfis `Sasuke` e `Naruto` com ritmo `+18%`).
   - `voice_studio.py` possui autostart do backend local no Windows (1 tentativa por execução, até 45 esperas de 2 s), cache em memória do áudio bruto de referência (`_ref_cache`, TTL 120 s) e preparo único por voz (`_prepared_reference`: corte de 10 s, 24 kHz, mono + transcrição Whisper cacheada em disco `cache/ref_cache/<sha256[:24]>.wav` e `.txt` e em memória).
4. **Alinhamento de Legendas (`captions.py`):**
   - Usa `faster-whisper` (`small` por padrão em `settings.json`, `local_files_only=True`) para alinhar palavra por palavra e gerar legendas `.ass` transparentes (máx. 3 palavras por bloco, texto branco com contorno preto e palavra ativa destacada em amarelo/ciano).
   - `captions.match_words`: palavras interpoladas com duração inferior a 0,08 s ganham pelo menos 0,08 s (`expand_interpolated_words`), tomando tempo das vizinhas e preservando a ordem cronológica e o fim da fala.
   - `captions.whisper_words`: se o modelo Whisper não estiver baixado localmente, levanta `RuntimeError` em português com o comando exato e referência a `instalar_legendas.bat`, sem traceback do HuggingFace.
   - `instalar_legendas.bat`: lê o modelo de `settings.json` (`"whisper_model"`) e baixa automaticamente para `output/alignment_cache/models`.
5. **Renderização (`render_pipeline.py` + `engine.py`):**
   - `engine.character()` redimensiona o sprite com `thumbnail((510, 640))` e gera sombra suave projetada atrás do sprite (`GaussianBlur(9)`, opacidade `0.62`, deslocamento `+6px` para baixo) com cache em `output/cache/sprite_v2_<indice>_<mtime_ns>.png`.
   - `render_pipeline.py` usa `-threads 0`, aplica `vignette=PI/6` no fundo, `highpass=f=80,acompressor=threshold=-18dB:ratio=2.2:attack=10:release=120` antes do `alimiter` nas vozes e `loudnorm=I=-14:TP=-1.5:LRA=11` no áudio final em 30 FPS padrão.

## Privacidade dos Assets

- Os arquivos `assets/characters/*.png` (`sasuke.png` e `naruto.png`), `assets/demo*/voice_*.wav` e `fala_*.wav` estão no `.gitignore` e não são rastreados no índice do repositório (permanecem apenas no disco local do usuário).
- O script `publicar_github.py` cria/atualiza o repositório como **privado por padrão** (use `--publico` para público), lê o token exclusivamente de `GITHUB_TOKEN` ou `getpass` (sem argumento `--token` e sem eco no terminal) e faz push sem `force`.
- Testes que utilizam áudios de demonstração sintetizam automaticamente um WAV silencioso via módulo padrão `wave` quando o arquivo local não existir.

## Suíte de Testes (61 Testes)

- `tests/test_news_auto.py` (36 testes) e `tests/test_integration.py` (25 testes) — totalizando **61 testes** automatizados (com `unittest.skipUnless` quando `tkinter`, `numpy` ou arquivos locais de demonstração não estiverem presentes).

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m unittest tests.test_news_auto tests.test_integration -v
```
