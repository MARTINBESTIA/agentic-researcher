@echo off
REM ALTERNATIVA k Ollame: llama-swap s OpenAI API na 127.0.0.1:8080; modely sa menia podla pola "model".
REM Najprv skopiruj llama-swap.example.yaml ako llama-swap.yaml a uprav cesty (pozri komentare v subore).
REM V config.yaml potom nastav models.base_url: http://127.0.0.1:8080/v1 (pozri llama-swap.example.yaml).
set LLAMA_SWAP=C:\llama-swap\llama-swap.exe
"%LLAMA_SWAP%" --config llama-swap.yaml --listen 127.0.0.1:8080
