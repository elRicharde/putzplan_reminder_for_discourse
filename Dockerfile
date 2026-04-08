FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY putzplan_reminder.py .

ENTRYPOINT ["python", "putzplan_reminder.py"]
