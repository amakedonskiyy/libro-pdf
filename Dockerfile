FROM python:3.11-slim

# DejaVu шрифти (кирилиця для рендеру) + Tesseract OCR (рятує биті заголовки)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core fonts-dejavu-extra \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-ukr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pdf_translator.py main.py ./

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
