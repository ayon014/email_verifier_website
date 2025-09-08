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
import threading  # added for background worker
from werkzeug.utils import secure_filename
from dotenv import load_dotenv  # For environment variables

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for React frontend

# Configuration
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['RESULTS_FOLDER'] = 'results'

# Get API key and limits from environment variables with fallbacks
API_KEY = os.getenv('API_KEY', "f4488df31e8e4cf70b779feb674c23f146adf30d23f3923503b4584bfe6b")
MAX_EMAILS = int(os.getenv('MAX_EMAILS', 100))  # Configurable limit

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['RESULTS_FOLDER'], exist_ok=True)


@app.route('/api/validate', methods=['POST'])
def validate_emails():
    # Check if file was uploaded
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Generate a unique session ID for this validation
    session_id = str(uuid.uuid4())

    # Save uploaded file
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{session_id}_{filename}")
    file.save(file_path)

    # Read emails from file
    try:
        emails = read_emails(file_path)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if not emails:
        return jsonify({'error': 'No emails found in the file'}), 400

    # Check if file exceeds the allowed limit
    if len(emails) > MAX_EMAILS:
        return jsonify({
            'error': f'File contains {len(emails)} emails, but the maximum allowed is {MAX_EMAILS}. Please upload a smaller file or upgrade your plan.'
        }), 400

    # Prepare initial progress file
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

    # Background worker to process emails
    def _process():
        results = {}
        for i, email in enumerate(emails, 1):
            status, reason = validate_email(email)
            results[email] = (status, reason)

            # Update progress
            validation_progress['processed'] = i
            validation_progress['percentage'] = (i / len(emails)) * 100

            with open(progress_file, 'w') as f:
                json.dump(validation_progress, f)

            time.sleep(0.1)  # avoid overwhelming the API

        # Save results
        valid_count, invalid_count = save_results(session_id, results)

        # Mark as complete
        validation_progress['status'] = 'complete'
        validation_progress['valid_count'] = valid_count
        validation_progress['invalid_count'] = invalid_count
        with open(progress_file, 'w') as f:
            json.dump(validation_progress, f)

    threading.Thread(target=_process, daemon=True).start()

    # Return session info immediately
    return jsonify({
        'session_id': session_id,
        'total': len(emails),
        'valid_count': 0,
        'invalid_count': 0,
        'limit': MAX_EMAILS
    })


@app.route('/api/limits')
def get_limits():
    """Endpoint to get current validation limits"""
    return jsonify({
        'max_emails': MAX_EMAILS,
        'api_key_set': bool(API_KEY)
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

    # Try to detect email column
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


if __name__ == '__main__':
    app.run(debug=True)
