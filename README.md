# ReelForge com VoiceStudio e gameplay aleatória

Extraia o projeto e execute `iniciar_windows.bat`. Use **Exemplo offline** e **Gerar Reel** para testar as falas incluídas.

Para roteiros novos, mantenha o Ollama (`http://localhost:11434`, modelo do redator `qwen3.5:4b`, juiz `qwen3:8b`) e o VoiceStudio (`http://127.0.0.1:3900`) abertos com os perfis `Sasuke` e `Naruto` disponíveis. Na aba **Vozes e legendas**, selecione `voicestudio` e informe os nomes ou IDs dos perfis. Os dois ritmos começam em `+18%`, com ajuste de tom em `0`. A prévia e a renderização reutilizam as falas geradas para o mesmo texto e perfil.

## Modelos do Ollama (Redator e Juiz de Qualidade)

- **Redator padrão:** `qwen3.5:4b` (`ollama pull qwen3.5:4b`).
- **Juiz de qualidade e checagem factual:** `qwen3:8b` (`ollama pull qwen3:8b`), configurado como `"judge_model": "qwen3:8b"` em `settings.json`. Se o modelo de 8B não estiver baixado no Ollama (HTTP 404), o ReelForge automaticamente faz fallback para o modelo do redator e registra `"juiz_fraco": true` nos metadados do roteiro e do Reel.

## Personagens e Vozes de Referência (Fornecidos pelo Usuário)

Por motivos de privacidade e direitos de imagem/voz, os sprites dos personagens e os áudios de voz **não são rastreados no repositório Git** (estão listados no `.gitignore`):

- **Sprites dos personagens (`assets/characters/*.png`):** fornecidos localmente pelo usuário em `assets/characters/` (`character_0_closed.png`, `character_0_open.png`, `character_1_closed.png`, `character_1_open.png`).
- **Vozes de referência e amostras (`VoiceStudio` / `assets/demo*/voice_*.wav` e `fala_*.wav`):** fornecidas localmente pelo usuário nos perfis do VoiceStudio (`Sasuke` e `Naruto`) ou nas pastas de demonstração local.

## Como baixar o modelo Whisper (`small`) para uso offline (`local_files_only`)

Por padrão, o `settings.json` utiliza o modelo `small` do Faster-Whisper com `local_files_only=True` (para não acessar a internet durante a renderização). Para baixar o modelo `small` uma única vez para a pasta local `output/alignment_cache/models`, execute no PowerShell dentro da pasta do projeto:

```powershell
.\.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8', download_root='output/alignment_cache/models')"
```

Caso queira usar o modelo `base` já instalado em `output/alignment_cache/models`, basta selecionar `base` na aba **Vozes e legendas** ou alterar `"whisper_model": "base"` em `settings.json`.

## Vídeo, Loop e Áudio

- **Taxa de quadros:** O padrão é **30 FPS** (com opção de **60 FPS** mantida na interface e na CLI via `--fps 60`).
- **Normalização de áudio:** O áudio final aplica `highpass=f=80,acompressor` na mistura das vozes e `loudnorm` (`I=-14, TP=-1.5`) na saída final com a música de fundo.
- **Gameplay de fundo:** O fundo padrão é `assets/gameplay/gameplay_principal.mp4`, sorteando um trecho diferente a cada Reel (`gameplay_start` no JSON de saída), com brilho `-0,06`, gama `0,85`, `vignette=PI/6` e crossfade visual/musical no loop (`"loop": true`). A ponte falada no texto fica desligada por padrão (`"script_loop": false` em `settings.json`).

Fonte da gameplay: https://youtu.be/zxmENLsNzPo
