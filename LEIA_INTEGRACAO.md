# ReelForge com VoiceStudio e gameplay aleatória

Extraia o ZIP e execute iniciar_windows.bat. Use Abrir exemplo sem API e Gerar Reel para testar as duas falas já incluídas.

Para roteiros novos, abra o VoiceStudio e mantenha os perfis Sasuke e Naruto disponíveis. Na aba Vozes e legendas, selecione voicestudio e informe os nomes ou IDs dos perfis. Os dois ritmos começam em +18%, com ajuste de tom em 0. A síntese usa a API local e o primeiro uso pode baixar o modelo Whisper para transcrever a referência. A prévia e a renderização reutilizam as falas geradas para o mesmo texto e perfil.

O fundo padrão é o vídeo completo em assets/gameplay/gameplay_principal.mp4. A cada renderização, o gerador sorteia um início com espaço para a duração do Reel. O ponto escolhido fica em gameplay_start no JSON de saída. Se você selecionar um fundo menor do que o Reel, ele se repete.

Na aba Vídeo e duração, ajuste brilho e gama da gameplay. Padrões: -0,06 e 0,85. Valores neutros: 0 e 1.

Fonte da gameplay: https://youtu.be/zxmENLsNzPo


## Notícias automáticas (RSS com matéria completa + Google News RSS + Ollama local)

Sem fonte colada, "Criar plano + roteiro" e "Gerar Reel" fazem tudo sozinhos:

1. Leem em paralelo os feeds RSS de veículos brasileiros que publicam o texto completo da matéria no próprio XML (g1, CNN Brasil, Agência Brasil, Poder360, InfoMoney, Tecnoblog, Canaltech, Olhar Digital, ge) e o Google News RSS (pt-BR). A internet é usada somente para consultar esses feeds RSS.
2. Descartam posts de redes sociais, propagandas/cupons, títulos com emojis e pautas já usadas (`history.sqlite3`).
3. O Ollama local (`qwen3.5:4b`, `http://localhost:11434`) escolhe a pauta priorizando matérias com dados concretos e explica o motivo no log.
4. O Ollama escreve o diálogo com alternância garantida Sasuke/Naruto e pelo menos 4 fatos concretos da matéria (números, nomes próprios, empresas, lugares). Números inventados ou roteiros genéricos são rejeitados e refeitos.
5. **Vídeo em loop (padrão ativado):** o roteiro usa número par de falas e termina com uma frase-ponte aberta (sem ponto final) que se completa na primeira fala; no vídeo, o final da gameplay e da música fazem crossfade com o início (`xfade` / `acrossfade`), conectando o fim ao começo sem corte seco.
6. **Música de fundo bem baixinha (padrão 8%):** sorteia um arquivo de `assets\music` (MP3, WAV, M4A, OGG, FLAC, AAC) ou, se a pasta estiver vazia, gera localmente uma faixa lo-fi suave (`assets\music\reelforge_lofi_gerada.wav`) com loop exato, sem usar internet.
7. Se a narração passar da duração máxima, o Ollama reescreve uma versão mais curta, sem cortar frases.
8. Depois de renderizada, a pauta é registrada e não volta a aparecer, a menos que você marque "Permitir conteúdo repetido".

Linha de comando: `.venv\Scripts\python.exe engine.py --generate --topic tecnologia --max-seconds 45`
(`--preview` só mostra a pauta e o roteiro). O CLI usa as vozes, o brilho, a gama, o loop e a música salvos em `settings.json`.

O VoiceStudio precisa estar aberto em `http://127.0.0.1:3900` para gerar novas falas.
