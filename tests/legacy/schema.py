"""Validate editable Jev schemas and generate proposals with Codex CLI."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

def validate_questions(schema):
    questions = schema.get('questions') if isinstance(schema, dict) else None
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 12:
        raise ValueError("El esquema necesita entre 1 y 12 preguntas en 'questions'.")
    for key, question in questions.items():
        if not isinstance(key, str) or not key or len(key) > 64 or not isinstance(question, dict):
            raise ValueError('Cada pregunta necesita un identificador y una definición válidos.')
        kind = question.get('type')
        if kind not in ('choice', 'score', 'noul'):
            raise ValueError(f'Tipo inválido en {key}: usa choice, score o noul.')
        if not isinstance(question.get('instructions'), str) or not question['instructions'].strip():
            raise ValueError(f'Faltan instrucciones en {key}.')
        criteria = question.get('criteria')
        if kind == 'choice' and (not isinstance(criteria, dict) or not 2 <= len(criteria) <= 12):
            raise ValueError(f'{key} necesita de 2 a 12 opciones en criteria.')
        if kind == 'score' and (not isinstance(criteria, list) or not 2 <= len(criteria) <= 12):
            raise ValueError(f'{key} necesita de 2 a 12 niveles en criteria.')
    return questions


def generate_schema(instructions):
    codex = shutil.which('codex.cmd' if os.name == 'nt' else 'codex')
    if not codex:
        raise RuntimeError('Codex CLI no está instalado o no aparece en PATH.')
    prompt = ("Genera un esquema de clasificación para Jev según las instrucciones del usuario. "
              "Responde únicamente JSON válido, sin markdown: un objeto con clave 'questions', "
              "que contiene un mapa de IDs cortos a preguntas. Cada pregunta tiene 'type' "
              "('choice', 'score' o 'noul'), 'instructions' y, para choice, 'criteria' como "
              "objeto de 2 a 12 etiquetas con descripciones; para score, una lista ordenada "
              "de 2 a 12 niveles. Usa noul para preguntas de sí/no. Crea de 1 a 5 preguntas. "
              "No ejecutes herramientas ni escribas archivos.\n\nInstrucciones del usuario:\n" + instructions)
    with tempfile.TemporaryDirectory(prefix='jev-schema-') as directory:
        output = Path(directory) / 'schema.json'
        command = [codex, 'exec', '--ephemeral', '--sandbox', 'read-only', '--skip-git-repo-check',
                   '--output-last-message', str(output), '-']
        try:
            result = subprocess.run(command, input=prompt, capture_output=True, text=True,
                                    encoding='utf-8', cwd=directory, timeout=150, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Codex CLI tardó demasiado en generar el esquema.') from exc
        if result.returncode != 0 or not output.exists():
            raise RuntimeError('Codex CLI no pudo generar el esquema. Revisa su sesión y conexión.')
        raw = output.read_text(encoding='utf-8').strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        try:
            schema = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError('Codex CLI devolvió un esquema inválido.') from exc
        validate_questions(schema)
        return schema
