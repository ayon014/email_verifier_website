# app.py
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import pandas as pd
import requests
import csv
import os
import uuid
import json
import time
import threading
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Load environment variables from .env (for local development)
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for React frontend

# Configuration
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['RESULTS_FOLDER'] = 'results'

# Get API key and limits from environment variables
# Raise error if missing
API_KEY = os.environ.get('API_KEY')
if not API_KEY:
    raise RuntimeError("API_KEY environment variable is not set!")

MAX_EMAILS_ENV = os.environ.get('MAX_EMAILS')
if not MAX_EMAILS_ENV:
    raise RuntimeError("MAX_EMAILS environment variable is not set!")
try:
    MAX_EMAILS = int(MAX_EMAILS_ENV)
except ValueError:
    raise ValueError("MAX_EMAILS environment variable must be an integer!")

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RESULTS_FOLDER'], exist_ok=True)


@app.route('/api/validate', methods=['POST'])
def validate_emails():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    session_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{session_id}_{filename}")
    file.save(file_path)

    try:
        emails = read_emails(file_path)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if not emails:
        return jsonify({'error': 'No emails found in the file'}), 400

    if len(emails) > MAX_EMAILS:
        return jsonify({
            'error': f'File contains {len(emails)} emails, maximum allowed is {MAX_EMAILS}.'
        }), 400

    progress_file = os.path.join(app.config['RESULTS_FOLDER'], f"{session_id}_progress.json")
    validation_progress = {
        'total': len(emails),
        'processed': 0,
        'session_id': session_id,
        'status': 'processing',
        'limit': MAX_EMAILS
    }
    with open(progress_file, 'w') as f:
        json.dump(validation_progress, f)

    def _process():
        results = {}
        for i, email in enumerate(emails, 1):
            status, reason = validate_email(email)
            results[email] = (status, reason)

            validation_progress['processed'] = i
            validation_progress['percentage'] = (i / len(emails)) * 100
            with open(progress_file, 'w') as f:
                json.dump(validation_progress, f)

            time.sleep(0.1)

        valid_count, invalid_count = save_results(session_id, results)
        validation_progress['status'] = 'complete'
        validation_progress['valid_count'] = valid_count
        validation_progress['invalid_count'] = invalid_count
        with open(progress_file, 'w') as f:
            json.dump(validation_progress, f)

    threading.Thread(target=_process, daemon=True).start()

    return jsonify({
        'session_id': session_id,
        'total': len(emails),
        'valid_count': 0,
        'invalid_count': 0,
        'limit': MAX_EMAILS
    })


@app.route('/api/limits')
def get_limits():
    return jsonify({
        'max_emails': MAX_EMAILS,
        'api_key_set': True
    })


@app.route('/api/progress/<session_id>')
def get_progress(session_id):
    progress_file = os.path.join(app.config['RESULTS_FOLDER'], f"{session_id}_progress.json")
    if not os.path.exists(progress_file):
        return jsonify({'error': 'Session not found'}), 404
    with open(progress_file, 'r') as f:
        progress_data = json.load(f)
    return jsonify(progress_data)


@app.route('/api/download/<session_id>/<file_type>')
def download_results(session_id, file_type):
    if file_type not in ['valid', 'invalid']:
        return jsonify({'error': 'Invalid file type'}), 400

    file_path = os.path.join(app.config['RESULTS_FOLDER'], f"{session_id}_{file_type}_emails.csv")
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found'}), 404

    return send_file(file_path, as_attachment=True, download_name=f"{file_type}_emails.csv")


def read_emails(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(file_path)
    elif ext in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path)
    else:
        raise ValueError("Unsupported file type. Please upload a CSV or Excel file.")

    email_col = next((col for col in df.columns if "email" in col.lower()), df.columns[0])
    emails = df[email_col].dropna().astype(str).tolist()
    return emails[:MAX_EMAILS]


def validate_email(email):
    url = f"https://api.quickemailverification.com/v1/verify?email={email}&apikey={API_KEY}"
    try:
        resp = requests.get(url, timeout=20)
        data = resp.json()
        return data.get("result", "unknown"), data.get("reason", "")
    except Exception as e:
        return "error", str(e)


def save_results(session_id, results):
    valid = [e for e, (s, _) in results.items() if s == "valid"]
    invalid = [e for e, (s, _) in results.items() if s != "valid"]

    valid_file = os.path.join(app.config['RESULTS_FOLDER'], f"{session_id}_valid_emails.csv")
    invalid_file = os.path.join(app.config['RESULTS_FOLDER'], f"{session_id}_invalid_emails.csv")

    with open(valid_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "Status"])
        for e in valid:
            writer.writerow([e, "valid"])

    with open(invalid_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Email", "Status", "Reason"])
        for e in invalid:
            status, reason = results[e]
            writer.writerow([e, status, reason])

    return len(valid), len(invalid)


# -----------------------
# Production entry point
# -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
