
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from pathlib import Path
import os
import json
import re
import base64
import shutil
import subprocess
import tempfile

load_dotenv()

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
ARDUINO_CLI = os.getenv(
    "ARDUINO_CLI",
    r"C:\Users\Ranjith P\Downloads\arduino-cli_1.5.2-rc.1_Windows_64bit\arduino-cli.exe"
)
FQBN = os.getenv("ESP32_FQBN", "esp32:esp32:esp32")
COMPILE_TIMEOUT = int(os.getenv("COMPILE_TIMEOUT", "180"))

client = genai.Client(api_key=API_KEY) if API_KEY else None


def json_text(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def ask_ai(prompt):
    if not client:
        raise RuntimeError("GEMINI_API_KEY is missing")
    response = client.models.generate_content(model=MODEL, contents=prompt)
    return json.loads(json_text(response.text))


def ai_error(exc):
    message = str(exc)
    low = message.lower()
    if "429" in message or "quota" in low or "resource_exhausted" in low:
        return "Gemini quota exhausted"
    if "503" in message or "unavailable" in low:
        return "Gemini temporarily unavailable"
    return message


def success(data):
    return jsonify({"success": True, **data})


def failure(message, status=400, **extra):
    return jsonify({"success": False, "error": message, **extra}), status


def packed(path, address):
    return {
        "name": path.name,
        "address": address,
        "size": path.stat().st_size,
        "data": base64.b64encode(path.read_bytes()).decode("ascii")
    }


def detect_rules(text):
    low = text.lower()
    rules = []
    if "nan" in low:
        rules.append("NaN detected: invalid numeric data.")
    if "-127" in low:
        rules.append("-127 detected: common DS18B20 communication failure.")
    if "4095" in low:
        rules.append("4095 detected: possible ADC saturation or analog-input issue.")
    if "timeout" in low:
        rules.append("Timeout detected: possible communication or wiring issue.")
    if "no ack" in low or "not found" in low:
        rules.append("Device acknowledgement or discovery failed.")
    if "[ai_error]" in low or "[test_fail]" in low:
        rules.append("Firmware reported an explicit failure marker.")
    return rules


def compile_source(code):
    temp_dir = Path(tempfile.mkdtemp(prefix="ai_hardware_"))
    sketch_dir = temp_dir / "generated"
    sketch_dir.mkdir()
    (sketch_dir / "generated.ino").write_text(code, encoding="utf-8")

    try:
        result = subprocess.run(
            [
                ARDUINO_CLI,
                "compile",
                "--fqbn",
                FQBN,
                "--export-binaries",
                str(sketch_dir)
            ],
            cwd=sketch_dir,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT
        )

        output = "\n".join(
            item for item in (result.stdout, result.stderr) if item
        ).strip()

        binaries = list(sketch_dir.rglob("*.bin"))
        firmware = next(
            (
                p for p in binaries
                if p.name.endswith(".ino.bin")
                and "bootloader" not in p.name
                and "partitions" not in p.name
            ),
            None
        )
        bootloader = next(
            (p for p in binaries if "bootloader" in p.name),
            None
        )
        partitions = next(
            (p for p in binaries if "partitions" in p.name),
            None
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": output or "Compilation failed.",
                "output": output
            }

        if not firmware:
            return {
                "success": False,
                "error": "Firmware binary was not generated.",
                "output": output
            }

        data = {
            "success": True,
            "message": "Compilation successful.",
            "output": output,
            "firmware": packed(firmware, "0x10000")
        }

        if bootloader:
            data["bootloader"] = packed(bootloader, "0x1000")
        if partitions:
            data["partitions"] = packed(partitions, "0x8000")

        return data
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/health")
def health():
    return jsonify({
        "success": True,
        "arduino_cli": os.path.isfile(ARDUINO_CLI),
        "gemini": bool(client),
        "model": MODEL,
        "fqbn": FQBN
    })


@app.post("/plan_hardware")
def plan_hardware():
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()

    if not prompt:
        return failure("Project description is required.")

    ai_prompt = f"""
You are the hardware architect for a beginner-friendly ESP32 IDE.

Project:
{prompt}

Return ONLY valid JSON:
{{
  "project_title": "short title",
  "project_summary": "short practical summary",
  "categories": [
    {{
      "id": "unique-category-id",
      "name": "Microcontroller or sensor category",
      "purpose": "why it is needed",
      "options": [
        {{
          "id": "unique-option-id",
          "name": "exact part",
          "recommended": true,
          "price_inr": "₹400-₹700",
          "description": "short description",
          "reason": "why it fits"
        }}
      ]
    }}
  ]
}}

Rules:
Use ESP32 unless explicitly rejected.
Give 2 or 3 realistic options for each required category.
Include only required categories.
Use approximate INR prices.
Prefer safe ESP32 GPIO choices.
Do not invent unnecessary components.
"""
    try:
        return success(ask_ai(ai_prompt))
    except Exception as exc:
        return failure(ai_error(exc), 503)


@app.post("/build_project")
def build_project():
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    selected = data.get("selected_components", [])

    if not prompt:
        return failure("Project description is required.")
    if not isinstance(selected, list) or not selected:
        return failure("At least one hardware component must be selected.")

    ai_prompt = f"""
You are an ESP32 embedded-systems architect.

Project:
{prompt}

Selected hardware:
{json.dumps(selected, ensure_ascii=False, indent=2)}

Return ONLY valid JSON:
{{
  "chat_message": "short explanation",
  "components": [
    {{
      "id": "selected component id",
      "name": "component name",
      "type": "sensor|output|display|controller|other",
      "purpose": "purpose"
    }}
  ],
  "wiring": [
    {{
      "id": "wire-1",
      "component_id": "selected component id",
      "step": 1,
      "text": "clear connection instruction",
      "from": "source",
      "to": "destination",
      "verify": "specific verification"
    }}
  ],
  "pin_map": [
    {{
      "component_id": "selected component id",
      "gpio": 4,
      "signal": "DATA",
      "power": "3.3V",
      "ground": "GND",
      "interface": "digital|analog|i2c|spi|uart|onewire|other"
    }}
  ],
  "cpp_code": "complete Arduino C++ program"
}}

Rules:
The program must compile for {FQBN}.
Use Serial.begin(115200).
Use the selected hardware only.
Use explicit GPIO numbers.
Do not generate user yes/no popups for normal operation.
The system must diagnose inputs from telemetry automatically.
Use these machine-readable markers:
[AI_DIAG] BOOT_OK
[AI_DIAG] SYSTEM_READY
[AI_SENSOR] component_id=value
[AI_WARNING] component_id=reason
[AI_ERROR] component_id=reason
[AI_OUTPUT] component_id=command
Print meaningful sensor values repeatedly.
For invalid input readings print AI_ERROR.
Do not claim an output is physically working merely because software commanded it.
"""
    try:
        return success(ask_ai(ai_prompt))
    except Exception as exc:
        return failure(ai_error(exc), 503)


@app.post("/troubleshoot")
def troubleshoot():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    project = str(data.get("project", ""))
    code = str(data.get("cpp_code", ""))
    logs = str(data.get("serial_logs", ""))
    selected = data.get("selected_components", [])
    wiring = data.get("wiring", [])
    pin_map = data.get("pin_map", [])

    if not message:
        return failure("Troubleshooting message is required.")

    ai_prompt = f"""
You are the persistent troubleshooting assistant inside an ESP32 IDE.

User request:
{message}

Project:
{project}

Selected hardware:
{json.dumps(selected, ensure_ascii=False, indent=2)}

Wiring:
{json.dumps(wiring, ensure_ascii=False, indent=2)}

Pin map:
{json.dumps(pin_map, ensure_ascii=False, indent=2)}

Current C++:
{code}

Recent serial logs:
{logs[-16000:]}

Known deterministic evidence:
{json.dumps(detect_rules(logs), ensure_ascii=False)}

Return ONLY valid JSON:
{{
  "reply": "clear answer to the user",
  "issue_type": "SOFTWARE|HARDWARE|REQUEST|UNKNOWN",
  "change_type": "NONE|PATCH|FULL",
  "needs_permission": true,
  "patch_summary": "short description",
  "changed_lines": ["logical change 1", "logical change 2"],
  "patched_code": "complete replacement program for PATCH or empty string",
  "full_code": "complete replacement program for FULL or empty string",
  "physical_steps": ["specific hardware checks"],
  "diagnostic_reason": "evidence-based reason"
}}

Rules:
Prefer PATCH for a local timing, GPIO, threshold, formula, function, or parameter change.
Use FULL only when the user requests a full rewrite or a structural rewrite is genuinely necessary.
Never return partial code.
Never silently change code.
Do not invent a hardware failure.
"""
    try:
        return success(ask_ai(ai_prompt))
    except Exception as exc:
        return failure(ai_error(exc), 503)


@app.post("/diagnose")
def diagnose():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "runtime"))
    project = str(data.get("project", ""))
    code = str(data.get("cpp_code", ""))
    compiler_error = str(data.get("compiler_error", ""))
    logs = str(data.get("serial_logs", ""))
    selected = data.get("selected_components", [])
    wiring = data.get("wiring", [])
    pin_map = data.get("pin_map", [])

    ai_prompt = f"""
You are the AI Doctor for an ESP32 IDE.

Mode:
{mode}

Project:
{project}

Selected hardware:
{json.dumps(selected, ensure_ascii=False, indent=2)}

Wiring:
{json.dumps(wiring, ensure_ascii=False, indent=2)}

Pin map:
{json.dumps(pin_map, ensure_ascii=False, indent=2)}

Current code:
{code}

Compiler diagnostics:
{compiler_error}

Serial telemetry:
{logs[-16000:]}

Deterministic evidence:
{json.dumps(detect_rules(logs + compiler_error), ensure_ascii=False)}

Return ONLY valid JSON:
{{
  "fault_type": "SOFTWARE|HARDWARE|UNKNOWN",
  "severity": "LOW|MEDIUM|HIGH",
  "component_id": "specific selected component id or empty",
  "component": "specific component name or empty",
  "summary": "one sentence",
  "evidence": ["facts from the evidence"],
  "user_message": "beginner-friendly explanation",
  "physical_steps": ["specific checks"],
  "suggested_code": "complete corrected program only when a software fix is safe, otherwise empty string",
  "needs_permission": true
}}

Rules:
Compiler failures are SOFTWARE.
NaN, -127, ADC saturation, missing acknowledgements, timeouts, and explicit firmware failure markers can indicate HARDWARE when the evidence supports it.
Use UNKNOWN when the evidence is insufficient.
Never silently modify code.
"""
    try:
        return success(ask_ai(ai_prompt))
    except Exception as exc:
        return failure(ai_error(exc), 503, deterministic_evidence=detect_rules(logs + compiler_error))


@app.post("/commission_code")
def commission_code():
    data = request.get_json(silent=True) or {}
    project = str(data.get("project", "")).strip()
    component = data.get("component", {})
    selected = data.get("selected_components", [])

    if not project or not component:
        return failure("Project and component are required.")

    component_id = str(component.get("id", "component"))
    component_name = str(component.get("name", "component"))

    ai_prompt = f"""
You are an ESP32 hardware commissioning engineer.

Final project:
{project}

Component being tested:
{json.dumps(component, ensure_ascii=False, indent=2)}

Other selected hardware:
{json.dumps(selected, ensure_ascii=False, indent=2)}

Create a temporary isolated unit-test firmware for ONLY the selected component.

Return ONLY valid JSON:
{{
  "title": "short test title",
  "purpose": "what the test proves",
  "wiring": [
    {{"step": 1, "text": "temporary test wiring"}}
  ],
  "cpp_code": "complete Arduino C++ test sketch",
  "expected": ["expected serial evidence"]
}}

Rules:
Use ESP32 and Serial.begin(115200).
This is a temporary commissioning test and must not change the main application architecture.
Do not ask the user whether the hardware works.
Use multiple sensor samples when applicable.
Use actual communication/read tests for I2C, SPI, UART, OneWire and digital sensors where applicable.
Use these exact markers:
[TEST_BEGIN] component={component_id}
[TEST_VALUE] component={component_id} value=value
[TEST_PASS] component={component_id} reason=reason
[TEST_FAIL] component={component_id} reason=reason
[TEST_UNKNOWN] component={component_id} reason=reason
[TEST_DONE] component={component_id}
For sensors and inputs, PASS requires actual valid data.
For outputs without feedback hardware, return UNKNOWN rather than claiming physical success.
"""
    try:
        return success(ask_ai(ai_prompt))
    except Exception as exc:
        return failure(ai_error(exc), 503)


@app.post("/compile")
def compile_code():
    data = request.get_json(silent=True) or {}
    code = str(data.get("cpp_code", ""))

    if not code.strip():
        return failure("C++ code is empty.")
    if not os.path.isfile(ARDUINO_CLI):
        return failure(f"Arduino CLI not found: {ARDUINO_CLI}", 500)

    try:
        result = compile_source(code)
        return jsonify(result), 200 if result.get("success") else 400
    except subprocess.TimeoutExpired:
        return failure("Compilation timed out.", 504)
    except Exception as exc:
        return failure(str(exc), 500)


@app.post("/compile_and_diagnose")
def compile_and_diagnose():
    data = request.get_json(silent=True) or {}
    code = str(data.get("cpp_code", ""))

    if not code.strip():
        return failure("C++ code is empty.")

    try:
        result = compile_source(code)
        if result.get("success"):
            return jsonify(result)

        diagnosis = None
        if client:
            prompt = f"""
You are an ESP32 compiler doctor.
Project: {data.get("project", "")}
Selected hardware: {json.dumps(data.get("selected_components", []), ensure_ascii=False)}
Compiler error:
{result.get("error", "")}
Current C++:
{code}
Return ONLY valid JSON:
{{
  "fault_type":"SOFTWARE",
  "severity":"MEDIUM",
  "component_id":"",
  "component":"",
  "summary":"one sentence",
  "evidence":["..."],
  "user_message":"...",
  "physical_steps":[],
  "suggested_code":"complete corrected C++ program",
  "needs_permission":true
}}
"""
            try:
                diagnosis = ask_ai(prompt)
            except Exception as exc:
                diagnosis = {"error": ai_error(exc)}

        result["diagnosis"] = diagnosis
        return jsonify(result), 400
    except Exception as exc:
        return failure(str(exc), 500)


@app.post("/ask_ai")
def legacy_ask_ai():
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()

    if not prompt:
        return failure("Project description is required.")

    ai_prompt = f"""
You are an ESP32 hardware mentor.
Project:
{prompt}

Return ONLY valid JSON:
{{
  "chat_message":"short explanation",
  "components":[{{"name":"...","rank":"Recommended","price":"₹..."}}],
  "instructions":[{{"step":"1","text":"..."}}],
  "cpp_code":"complete Arduino C++ code"
}}
Use ESP32 and Serial.begin(115200).
"""
    try:
        return success(ask_ai(ai_prompt))
    except Exception as exc:
        return failure(ai_error(exc), 503)


@app.errorhandler(Exception)
def server_error(exc):
    app.logger.exception("Unhandled server error")
    return jsonify({
        "success": False,
        "error": ai_error(exc)
    }), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
