# PDF & DOCX Comparator — Backend

Flask-сервер для сравнения содержимого DOCX и PDF файлов.

## Как это работает

1. **DOCX** → текст через `docx2txt`
2. **PDF** → распознаётся **двумя моделями**: PaddleOCR (основная) и Tesseract (контрольная)
3. Сравнение выполняется по тексту PaddleOCR
4. Если **обе** модели показывают расхождение — возвращается результат Tesseract (HTTP 422)
5. Тексты нормализуются (без пробелов, пунктуации, символов таблиц) и сравниваются

## Требования

- **Python 3.10–3.12** (обязательно для PaddleOCR; на 3.14 `paddlepaddle` не ставится)
- **[Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)**
- **PaddleOCR + paddlepaddle**

## Установка (Windows)

### 1. Python 3.12

Если установлен только Python 3.14:

```powershell
py install 3.12
```

### 2. Виртуальное окружение и зависимости

```powershell
cd "c:\Users\максим\Desktop\PDF&Docx_Comparator"
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-paddle.txt
copy .env.example .env
```

### 3. Tesseract

1. Скачайте: https://github.com/UB-Mannheim/tesseract/wiki
2. Установите с языками **Russian** и **English**
3. В `.env` укажите путь (если не в PATH):

```
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
TESSERACT_LANG=rus+eng
PADDLEOCR_LANG=ru
```

## Запуск

```powershell
.venv\Scripts\activate
python run.py
```

Проверка: `http://localhost:5000/api/health`

## API

| Метод | Endpoint       | Описание              |
|-------|----------------|-----------------------|
| GET   | `/api/health`  | Проверка работы       |
| POST  | `/api/compare` | Сравнение двух файлов |

```powershell
curl -X POST http://localhost:5000/api/compare `
  -F "file1=@document.docx" `
  -F "file2=@document.pdf"
```

Спецификация OpenAPI: [`swaggerUI.json`](swaggerUI.json)
