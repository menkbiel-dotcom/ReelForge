# MoneyPrinterTurbo

Referência: https://github.com/harry0703/MoneyPrinterTurbo

Commit consultado: `ab6004e5858998b2ce99ac46c0f16675eea9ab6f`.

Copyright (c) 2024 Harry. Licença MIT, reproduzida integralmente em `LICENSE-MoneyPrinterTurbo.txt`.

Integração seletiva, sem instalar ou executar o aplicativo MoneyPrinterTurbo:

- `captions.py`: fluxo Faster-Whisper com `word_timestamps=True`, agrupamento das palavras e adaptação da curva de entrada elástica de `app/services/video.py`.
- `speech.py`: organização em adaptadores de voz, reaproveitamento de áudio e tratamento explícito de falhas, a partir da arquitetura de `app/services/voice.py`. Implementação reduzida e adaptada ao ReelForge.
- `render_pipeline.py`: composição própria do ReelForge com FFmpeg, preservando transparência e um personagem por vez.

Arquivos consultados, com versão fixa:

- https://github.com/harry0703/MoneyPrinterTurbo/blob/ab6004e5858998b2ce99ac46c0f16675eea9ab6f/app/services/subtitle.py
- https://github.com/harry0703/MoneyPrinterTurbo/blob/ab6004e5858998b2ce99ac46c0f16675eea9ab6f/app/services/video.py
- https://github.com/harry0703/MoneyPrinterTurbo/blob/ab6004e5858998b2ce99ac46c0f16675eea9ab6f/app/services/voice.py

APIs implementadas conforme a documentação dos serviços:

- https://elevenlabs.io/docs/api-reference/text-to-speech/convert
- https://fish.audio/developers/

A licença do software não licencia as vozes, ilustrações ou gravações de terceiros. O pacote contém as ilustrações geradas e gravações de teste produzidas nas etapas anteriores deste projeto; não contém modelos de voz clonados.
