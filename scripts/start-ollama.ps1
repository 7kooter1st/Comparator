# Запуск Ollama с моделями из ASCII-пути (обход бага кириллицы в имени пользователя Windows)
$env:OLLAMA_MODELS = "C:\OllamaModels"

Get-Process ollama*, llama-server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "OLLAMA_MODELS = $env:OLLAMA_MODELS"
Write-Host "Запуск ollama serve..."
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
