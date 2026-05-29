from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.services.ai.instruction_assistant import InstructionAssistant
from app.services.conversion_service import ConversionService
from app.utils.rules_store import RulesStore
from app.utils.training_examples_store import TrainingExamplesStore


api_bp = Blueprint("api", __name__)
ALLOWED_EXCEL_EXTENSIONS = {".xlsx", ".xls"}


def _sanitize_filename(name: str) -> str:
    name = os.path.basename(name)
    forbidden = '<>:"/\\\\|?*'
    return "".join("_" if ch in forbidden else ch for ch in name)


def _is_excel_filename(name: str) -> bool:
    return Path(name).suffix.lower() in ALLOWED_EXCEL_EXTENSIONS


@api_bp.get("/ping")
def ping():
    return {"message": "pong"}


@api_bp.post("/uploads")
def upload_file():
    if "file" not in request.files:
        return {"error": "Файл не найден в запросе"}, 400
    incoming = request.files["file"]
    if incoming.filename == "":
        return {"error": "Пустое имя файла"}, 400
    if not _is_excel_filename(incoming.filename):
        return {"error": "Загрузите файл Excel в формате .xlsx или .xls"}, 400

    file_id = str(uuid.uuid4())
    original_filename = _sanitize_filename(incoming.filename)
    filename = f"{file_id}__{original_filename}"
    target_dir = Path(current_app.config["INPUT_DIR"])
    target_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = target_dir / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    incoming.save(target_path)
    (meta_dir / f"{file_id}.meta.json").write_text(
        json.dumps(
            {"job_id": file_id, "filename": filename, "original_filename": original_filename},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return jsonify(
        {
            "job_id": file_id,
            "filename": incoming.filename,
            "stored_as": filename,
            "size_bytes": target_path.stat().st_size,
            "status": "uploaded",
        }
    ), 201


@api_bp.post("/convert/<job_id>")
def convert(job_id: str):
    payload = request.get_json(silent=True) or {}
    schema = payload.get("schema")
    options = payload.get("options", {})
    service = ConversionService()
    result = service.convert(job_id=job_id, schema=schema, options=options)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 202


@api_bp.post("/convert-with-instruction/<job_id>")
def convert_with_instruction(job_id: str):
    payload = request.get_json(silent=True) or {}
    instruction = (payload.get("instruction") or "").strip()
    generated_rule = payload.get("generated_rule") or {}
    if not instruction:
        return jsonify({"error": "Передайте instruction"}), 400
    result = ConversionService().convert_with_instruction(job_id, instruction, generated_rule=generated_rule)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 202


@api_bp.post("/assistant/analyze")
def analyze_with_assistant():
    incoming = request.files.get("file")
    if not incoming or incoming.filename == "":
        return jsonify({"error": "Передайте Excel-файл в поле file"}), 400
    if not _is_excel_filename(incoming.filename):
        return jsonify({"error": "Загрузите файл Excel в формате .xlsx или .xls"}), 400
    raw_prompt = request.form.get("raw_prompt", "")
    sheet_name = request.form.get("sheet_name") or None
    with tempfile.TemporaryDirectory(prefix="assistant_api_") as tmp:
        path = Path(tmp) / _sanitize_filename(incoming.filename)
        incoming.save(path)
        result = InstructionAssistant().prepare_instruction(path, raw_prompt, sheet_name=sheet_name)
        similar_rules = RulesStore().find_similar_rules((result.get("analysis") or {}).get("fingerprint") or {})
        result["similar_rules"] = similar_rules
        return jsonify(result), 200


@api_bp.post("/rules")
def create_rule():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    prompt = (payload.get("prompt") or payload.get("ai_improved_instruction") or "").strip()
    if not name or not prompt:
        return jsonify({"error": "Передайте name и prompt"}), 400
    rule = RulesStore().add_rule(
        name=name,
        prompt=prompt,
        raw_prompt=payload.get("raw_user_prompt"),
        generated_rule=payload.get("generated_rule") or {},
        fingerprint=payload.get("fingerprint") or {},
        description=payload.get("description"),
        domain=payload.get("domain") or "universal",
        use_raw_data=payload.get("use_raw_data", True),
        sheet_name=payload.get("sheet_name"),
    )
    return jsonify(rule), 201


@api_bp.post("/training/batch")
def import_training_batch():
    archive = request.files.get("batch_zip")
    if not archive or archive.filename == "":
        return jsonify({"error": "Передайте файл batch_zip"}), 400
    if Path(archive.filename).suffix.lower() != ".zip":
        return jsonify({"error": "Загрузите ZIP-архив с обучающими парами"}), 400
    rule_id = request.form.get("rule_id") or None
    prompt = request.form.get("prompt", "").strip() or None
    store = TrainingExamplesStore()
    with tempfile.TemporaryDirectory(prefix="training_api_zip_") as tmp:
        zip_path = Path(tmp) / _sanitize_filename(archive.filename)
        archive.save(zip_path)
        summary = store.import_from_zip(zip_path, rule_id=rule_id, prompt=prompt)
    status = 201 if summary.get("added", 0) > 0 else 200
    return jsonify(summary), status
