from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
import os
import json
import subprocess
import tempfile
import shutil
import base64
from pathlib import Path

load_dotenv()

app = Flask(__name__)
CORS(app)

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("GEMINI_API_KEY not found in .env")

client = genai.Client(api_key=api_key)

ARDUINO_CLI = r"C:\Users\Ranjith P\Downloads\arduino-cli_1.5.2-rc.1_Windows_64bit\arduino-cli.exe"
ESP32_FQBN = "esp32:esp32:esp32"


@app.route("/ask_ai", methods=["POST"])
def ask_ai():

    data = request.get_json()
    prompt = data.get("prompt", "")

    if not prompt.strip():
        return jsonify({
            "error": "No project description provided"
        }), 400

    ai_prompt = f"""
You are an AI Hardware Mentor.

The user wants to build this hardware project:

{prompt}

Design the project using an ESP32.

Return ONLY valid JSON.
Do not use markdown.
Do not use ```json.
Do not add any explanation outside the JSON.

Use exactly this structure:

{{
    "chat_message": "Short explanation of the proposed project",
    "components": [
        {{
            "name": "Component name",
            "rank": "Recommended",
            "price": "Approximate price in INR"
        }}
    ],
    "instructions": [
        {{
            "step": "1",
            "text": "Clear wiring instruction"
        }}
    ],
    "cpp_code": "Complete Arduino C++ code for ESP32"
}}

Requirements:

1. Select realistic components for the project.
2. Include the ESP32.
3. Give practical GPIO connections.
4. Give clear step-by-step wiring.
5. Generate complete Arduino-compatible C++ code.
6. Use Serial.begin(115200).
7. The code should actually implement the requested project.
8. Do not invent components that are unnecessary.
9. Prices should be approximate and in INR.
10. Keep the explanation short.
11. Do not include comments in the generated C++ code.
12. Avoid using libraries unless they are necessary.
"""

    try:

        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=ai_prompt
        )

        text = response.text.strip()

        if text.startswith("```json"):
            text = text[7:]

        if text.startswith("```"):
            text = text[3:]

        if text.endswith("```"):
            text = text[:-3]

        text = text.strip()

        result = json.loads(text)

        return jsonify(result)

    except json.JSONDecodeError:

        return jsonify({
            "error": "Gemini returned invalid JSON",
            "raw_response": response.text
        }), 500

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


@app.route("/compile", methods=["POST"])
def compile_code():

    data = request.get_json()

    if not data:
        return jsonify({
            "success": False,
            "error": "No request data received"
        }), 400

    cpp_code = data.get("cpp_code", "")

    if not cpp_code.strip():
        return jsonify({
            "success": False,
            "error": "No C++ code provided"
        }), 400

    if not os.path.exists(ARDUINO_CLI):
        return jsonify({
            "success": False,
            "error": "Arduino CLI executable not found"
        }), 500

    temp_dir = Path(
        tempfile.mkdtemp(prefix="ai_hardware_")
    )

    sketch_dir = temp_dir / "generated"
    sketch_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    ino_file = sketch_dir / "generated.ino"

    try:

        ino_file.write_text(
            cpp_code,
            encoding="utf-8"
        )

        command = [
            ARDUINO_CLI,
            "compile",
            "--fqbn",
            ESP32_FQBN,
            "--export-binaries",
            str(sketch_dir)
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120
        )

        build_dir = (
            sketch_dir
            / "build"
            / "esp32.esp32.esp32"
        )

        firmware = build_dir / "generated.ino.bin"
        bootloader = build_dir / "generated.ino.bootloader.bin"
        partitions = build_dir / "generated.ino.partitions.bin"

        if result.returncode != 0:

            error_output = result.stderr.strip()

            if not error_output:
                error_output = result.stdout.strip()

            return jsonify({
                "success": False,
                "error": "Compilation failed",
                "details": error_output
            }), 400

        if not firmware.exists():

            return jsonify({
                "success": False,
                "error": "Firmware binary was not generated"
            }), 500

        if not bootloader.exists():

            return jsonify({
                "success": False,
                "error": "Bootloader binary was not generated"
            }), 500

        if not partitions.exists():

            return jsonify({
                "success": False,
                "error": "Partition binary was not generated"
            }), 500

        firmware_data = base64.b64encode(
            firmware.read_bytes()
        ).decode("ascii")

        bootloader_data = base64.b64encode(
            bootloader.read_bytes()
        ).decode("ascii")

        partitions_data = base64.b64encode(
            partitions.read_bytes()
        ).decode("ascii")

        return jsonify({

            "success": True,

            "message": "Compilation successful",

            "board": {
                "fqbn": ESP32_FQBN,
                "port": "COM8"
            },

            "firmware": {
                "data": firmware_data,
                "address": "0x10000",
                "size": firmware.stat().st_size
            },

            "bootloader": {
                "data": bootloader_data,
                "address": "0x1000",
                "size": bootloader.stat().st_size
            },

            "partitions": {
                "data": partitions_data,
                "address": "0x8000",
                "size": partitions.stat().st_size
            },

            "output": result.stdout

        })

    except subprocess.TimeoutExpired:

        return jsonify({
            "success": False,
            "error": "Compilation timed out"
        }), 500

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "ok",
        "arduino_cli": os.path.exists(ARDUINO_CLI),
        "fqbn": ESP32_FQBN
    })


if __name__ == "__main__":

    app.run(
        host="localhost",
        port=5000,
        debug=True
    )
