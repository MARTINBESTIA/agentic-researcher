@echo off
REM ALTERNATIVA k Ollame: vacsi MoE model cez llama-server (llama.cpp s podporou CUDA).
REM Pouzi ho, ak 9B model v Ollame nestaci kvalitou. V config.yaml potom nastav
REM llm.base_url: http://localhost:8080/v1 a llm.extra_body.chat_template_kwargs.enable_thinking: false.
REM
REM Uprav cesty podla toho, kam si rozbalil llama.cpp a stiahol model.
REM Hodnotu --n-cpu-moe najdi cez:
REM   llama-bench -m MODEL -ngl 99 --n-cpu-moe 20,25,30,35
REM a vyber najrychlejsiu, pri ktorej ostane asi 1 GB volnej VRAM.
REM Model Q4_K_M ma okolo 21 GB a vacsina z neho je v RAM: pred behom zatvor prehliadac,
REM Discord a podobne (pri 32 GB RAM inak hrozi odkladanie na disk a mnohonasobne spomalenie).
REM --cache-ram obmedzuje cache promptov v RAM (MiB); bez neho pri Qwen3.5/3.6 rastie o ~100 MB na prompt.

set LLAMA_SERVER=C:\llama.cpp\llama-server.exe
set MODEL=C:\models\Qwen3.6-35B-A3B-Q4_K_M.gguf

"%LLAMA_SERVER%" -m "%MODEL%" -ngl 99 --n-cpu-moe 30 -c 16384 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --cache-ram 2048 --jinja --host 127.0.0.1 --port 8080
