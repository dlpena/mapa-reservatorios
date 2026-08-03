@echo off
rem Gera o mapa e publica no GitHub Pages. Agendado diariamente as 12:00.
cd /d "C:\Users\diego\Projects\claude-code\mapa-reservatorios"

"C:\Users\diego\Projects\claude-code\app bancos ANA\.venv\Scripts\python.exe" gera_mapa.py
if errorlevel 1 (
    echo %date% %time% geracao falhou - nada publicado >> logs\publica.log
    powershell -NoProfile -ExecutionPolicy Bypass -File notifica_falha.ps1 -Mensagem "Geracao do mapa falhou - nada publicado. Ver logs\gera_mapa.log"
    exit /b 1
)

git add docs
git diff --cached --quiet
if not errorlevel 1 (
    echo %date% %time% sem mudancas - nada a publicar >> logs\publica.log
    exit /b 0
)
git commit -m "atualiza mapa %date%" >> logs\publica.log 2>&1
git push origin main >> logs\publica.log 2>&1
if errorlevel 1 (
    echo %date% %time% push falhou >> logs\publica.log
    powershell -NoProfile -ExecutionPolicy Bypass -File notifica_falha.ps1 -Mensagem "Push para o GitHub falhou - mapa nao atualizado. Ver logs\publica.log"
    exit /b 1
)
echo %date% %time% publicado com sucesso >> logs\publica.log
