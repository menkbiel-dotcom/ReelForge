# CLAUDE.md — Guia de Arquitetura do ReelForge

Este repositório contém o **ReelForge**, um gerador local para Windows de Reels verticais (`1080x1920`, `30 FPS` padrão ou `60 FPS`) onde **Sasuke** (`speaker = 0`) e **Naruto** (`speaker = 1`) conversam de forma natural e informal sobre notícias reais com gameplay de parkour ao fundo.

## Fluxo Principal (100% Local exceto leitura de RSS)

1. **Coleta RSS (`news_auto.py`):**
   - Lê feeds RSS de veículos brasileiros que trazem o texto completo da matéria (`content:encoded` de g1, CNN Brasil, Agência Brasil, Poder360, InfoMoney, Tecnoblog, Canaltech, Olhar Digital, ge) + Google News RSS.
   - Ranqueia as pautas por densidade informativa (`complexity_score`: números, entidades, tamanho do texto e frescor).
2. **Seleção e Roteiro em 2 Passos + Filtro de Qualidade (`news_auto.py` + Ollama local `qwen3.5:4b`):**
   - `pick_story()`: O Ollama escolhe a melhor pauta com corpo completo.
   - **Passo 1 (`extract_facts()`):** O Ollama extrai de **5 a 6 fatos curtos** em JSON `{"fatos":[...]}` exclusivamente do título e corpo da matéria (`source_text`), sem usar manchetes relacionadas.
   - **Passo 2 (`write_script()`):** O Ollama escreve o diálogo entre Sasuke (direto e irônico) e Naruto (curioso e espontâneo) usando *few-shot examples* em português brasileiro informal.
   - **Validação (`validate_lines()`):**
     - Primeira fala deve ser uma provocação ou pergunta de até 10 palavras (nunca a manchete copiada nem iniciada por `"Mas..."`).
     - Proíbe citar o nome do veículo ou expressões de telejornal (`"confirmado por"`, `"segundo o"`, `"nesta segunda-feira"`).
     - Rejeita números inventados e qualquer entidade/número vindo apenas de manchetes relacionadas (`enrich()`).
     - Exige que a última fala termine com uma frase completa (pergunta aberta ou fecho), sem ponte truncada.
   - **Filtro de Qualidade (`evaluate_script()`):** O Ollama avalia o roteiro em `{"nota": 1-10, "problemas": [...]}`. Se `nota < 7`, regenera o roteiro (máx. 2 vezes) passando os problemas como correção e grava a nota no JSON final (`output/reel_*.json`).
3. **Síntese de Voz (`speech.py` + `voice_studio.py`):**
   - Lê o provedor e os perfis de `settings.json` (`voicestudio` em `http://127.0.0.1:3900`, perfis `Sasuke` e `Naruto` com ritmo `+18%`).
4. **Alinhamento de Legendas (`captions.py`):**
   - Usa `faster-whisper` (`small` por padrão em `settings.json`, `local_files_only=True`) para alinhar palavra por palavra e gerar legendas `.ass` transparentes (máx. 3 palavras por bloco, texto branco com contorno preto e palavra ativa destacada em amarelo/ciano).
5. **Renderização (`render_pipeline.py`):**
   - Sorteia um ponto aleatório de `assets/gameplay/gameplay_principal.mp4` (`brilho -0.06`, `gama 0.85`), aplica crossfade visual e musical de loop (`xfade` + `acrossfade`), anima a entrada lateral com mola amortecida de um personagem por vez, mistura música de fundo baixa (`music.py`, volume padrão `8%`) e aplica normalização de loudness `loudnorm=I=-14:TP=-1.5:LRA=11`.

## Mapa de Arquivos

- `news_auto.py` — Busca RSS, extração de fatos em 2 passos, validação estrita e filtro de nota (1–10).
- `engine.py` — CLI (`--check`, `--preview`, `--generate`), integração com Ollama local (`http://127.0.0.1:11434`), leitura de `settings.json` e preparação de sprites.
- `render_pipeline.py` — Pipeline FFmpeg de vídeo/áudio, janelas de turno dos personagens (`turns`), crossfade de loop e `loudnorm`.
- `captions.py` — Alinhamento Faster-Whisper (`match_words`, `_clean_recognized`, `_merge_split_words`) e geração de legendas `.ass`.
- `speech.py` — Gerenciamento de perfis de voz (lidos de `settings.json`) e cache de síntese.
- `voice_studio.py` — Cliente HTTP para a API local do VoiceStudio (`http://127.0.0.1:3900`).
- `director.py` — Modo opcional de direção criativa para textos colados manualmente na interface.
- `music.py` — Seleção ou geração local de trilha lo-fi contínua para música de fundo.
- `gui.py` — Interface gráfica Tkinter para Windows (tema Dark Studio).
- `settings.json` / `.env.example` — Configurações persistidas do projeto.
- `tests/test_news_auto.py` e `tests/test_integration.py` — Suíte de 37 testes unitários e de integração.

## Como Executar os Testes

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m unittest tests.test_news_auto tests.test_integration -v
```
