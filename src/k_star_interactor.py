import sys
import json
import time
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

import PyIndi
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QGroupBox, QGridLayout,
    QMessageBox, QTabWidget, QComboBox
)
from PyQt6.QtCore import QThread, pyqtSignal
from google import genai
from google.genai import types

try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False

# INDI Helper Functions

def state_name(state):
    return {
        PyIndi.IPS_IDLE: "IDLE",
        PyIndi.IPS_OK: "OK",
        PyIndi.IPS_BUSY: "BUSY",
        PyIndi.IPS_ALERT: "ALERT",
    }.get(state, f"UNKNOWN({state})")


def wait_for_object(getter, timeout, description, poll=0.1):
    start = time.time()
    while time.time() - start < timeout:
        result = getter()
        if result is not None:
            return result
        time.sleep(poll)
    raise TimeoutError(f"Timed out waiting for: {description}")


def wait_for_true(predicate, timeout, description, poll=0.1):
    start = time.time()
    while time.time() - start < timeout:
        if predicate() is True:
            return True
        time.sleep(poll)
    raise TimeoutError(f"Timed out waiting for: {description}")



MOUNT_DRIVERS = {
    "PyIndi Simulator (Telescope Simulator)": "Telescope Simulator",
    "LX200 Classic": "LX200 Classic",
}


# INDI Client

class IndiClient(PyIndi.BaseClient):
    def __init__(self, device_name):
        super().__init__()
        self.device_name = device_name
        self.device = None
        self.radec_state = None

    def newDevice(self, d):
        if d.getDeviceName() == self.device_name:
            self.device = d

    def updateProperty(self, p):
        if p.getName() == "EQUATORIAL_EOD_COORD" and p.getType() == PyIndi.INDI_NUMBER:
            nvp = p.getNumber()
            self.radec_state = nvp.getState()

    def newProperty(self, p): pass
    def removeProperty(self, p): pass
    def newMessage(self, d, m): pass
    def newBLOB(self, bp): pass
    def newSwitch(self, svp): pass
    def newNumber(self, nvp): pass
    def newText(self, tvp): pass
    def newLight(self, lvp): pass


class SlewWorker(QThread):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)
    coords_signal = pyqtSignal(float, float)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, host, port, device_name, ra, dec):
        super().__init__()
        self.host = host
        self.port = port
        self.device_name = device_name
        self.target_ra = ra
        self.target_dec = dec
        self._is_aborted = False

    def abort(self):
        self._is_aborted = True

    def run(self):
        CONNECT_TIMEOUT = 10
        SLEW_TIMEOUT = 120
        POLL_INTERVAL = 0.5

        client = IndiClient(self.device_name)
        client.setServer(self.host, self.port)

        if hasattr(client, "watchDevice"):
            client.watchDevice(self.device_name)

        self.log_signal.emit(f"[INFO] Connecting to INDI server at {self.host}:{self.port}...")
        if not client.connectServer():
            self.log_signal.emit(f"[ERROR] Could not connect to INDI server at {self.host}:{self.port}.")
            self.finished_signal.emit(False, "Could not connect to INDI server.")
            return

        try:
            device = wait_for_object(
                lambda: client.device,
                CONNECT_TIMEOUT,
                f"device '{self.device_name}' to appear"
            )
            wait_for_true(
                lambda: device.isConnected(),
                CONNECT_TIMEOUT,
                f"device '{self.device_name}' to be connected"
            )
        except TimeoutError as e:
            self.log_signal.emit(f"[ERROR] {e}")
            client.disconnectServer()
            self.finished_signal.emit(False, str(e))
            return

        self.log_signal.emit(f"[OK] Device '{self.device_name}' is connected.")
        self.status_signal.emit("Preparing Slew")

        park_switch = device.getSwitch("TELESCOPE_PARK")
        if park_switch is not None:
            for s in park_switch:
                if s.getName() == "PARK" and s.getState() == PyIndi.ISS_ON:
                    self.log_signal.emit("[WARN] Mount appears PARKED. Unparking before slew...")
                    for s2 in park_switch:
                        s2.setState(PyIndi.ISS_ON if s2.getName() == "UNPARK" else PyIndi.ISS_OFF)
                    client.sendNewSwitch(park_switch)
                    time.sleep(2)

        try:
            on_coord_set = wait_for_object(
                lambda: device.getSwitch("ON_COORD_SET"),
                CONNECT_TIMEOUT,
                "ON_COORD_SET property"
            )
        except TimeoutError as e:
            self.log_signal.emit(f"[ERROR] {e}")
            client.disconnectServer()
            self.finished_signal.emit(False, str(e))
            return

        for s in on_coord_set:
            s.setState(PyIndi.ISS_ON if s.getName() == "SLEW" else PyIndi.ISS_OFF)
        client.sendNewSwitch(on_coord_set)
        time.sleep(0.5)

        try:
            radec = wait_for_object(
                lambda: device.getNumber("EQUATORIAL_EOD_COORD"),
                CONNECT_TIMEOUT,
                "EQUATORIAL_EOD_COORD property"
            )
        except TimeoutError as e:
            self.log_signal.emit(f"[ERROR] {e}")
            client.disconnectServer()
            self.finished_signal.emit(False, str(e))
            return

        for n in radec:
            if n.getName() == "RA":
                n.setValue(self.target_ra)
            elif n.getName() == "DEC":
                n.setValue(self.target_dec)

        client.radec_state = None
        self.log_signal.emit(f"[INFO] Sending target: RA={self.target_ra:.4f}h, DEC={self.target_dec:.4f}° ...")
        client.sendNewNumber(radec)

        try:
            wait_for_true(
                lambda: client.radec_state == PyIndi.IPS_BUSY,
                5.0,
                "slew to begin (state -> BUSY)"
            )
            self.log_signal.emit("[OK] Mount reports BUSY — slew in progress.")
        except TimeoutError:
            self.log_signal.emit("[WARN] Never saw BUSY state. Polling live status...")

        self.status_signal.emit("Slewing...")

        start = time.time()
        last_print = 0
        success = False
        msg = ""

        while time.time() - start < SLEW_TIMEOUT:
            if self._is_aborted:
                abort_sw = device.getSwitch("TELESCOPE_ABORT_MOTION")
                if abort_sw:
                    for s in abort_sw: s.setState(PyIndi.ISS_ON)
                    client.sendNewSwitch(abort_sw)
                self.log_signal.emit("[WARN] Slew aborted by user.")
                msg = "Slew Aborted"
                break

            current = device.getNumber("EQUATORIAL_EOD_COORD")
            cur_ra = cur_dec = None
            for n in current:
                if n.getName() == "RA":
                    cur_ra = n.getValue()
                elif n.getName() == "DEC":
                    cur_dec = n.getValue()

            if cur_ra is not None and cur_dec is not None:
                self.coords_signal.emit(cur_ra, cur_dec)

            state = current.getState()

            if time.time() - last_print >= POLL_INTERVAL:
                self.log_signal.emit(f"    ... RA={cur_ra:.4f}h  DEC={cur_dec:.4f}°  state={state_name(state)}")
                last_print = time.time()

            if state == PyIndi.IPS_OK:
                self.log_signal.emit(f"[OK] Slew complete! Final position: RA={cur_ra:.4f}h, DEC={cur_dec:.4f}°")
                self.status_signal.emit("Tracking / Idle")
                success = True
                msg = "Slew Complete"
                break

            if state == PyIndi.IPS_ALERT:
                self.log_signal.emit("[ERROR] Driver reported ALERT state — slew failed.")
                self.status_signal.emit("Error (Alert)")
                msg = "Mount reported ALERT"
                break

            time.sleep(0.2)
        else:
            self.log_signal.emit("[WARN] Slew timed out.")
            msg = "Slew Timeout"

        client.disconnectServer()
        self.finished_signal.emit(success, msg)


class HorizonsWorker(QThread):

    HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"

    result_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)
    log_signal = pyqtSignal(str)

    def __init__(self, body_name, body_id, observer_lat=None, observer_lon=None, observer_elev_km=0.0):
        super().__init__()
        self.body_name = body_name
        self.body_id = body_id
        self.observer_lat = observer_lat
        self.observer_lon = observer_lon
        self.observer_elev_km = observer_elev_km

    def run(self):
        try:
            now = datetime.now(timezone.utc)
            start = now.strftime("%Y-%m-%d %H:%M")
            stop = (now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")

            if self.observer_lat is not None and self.observer_lon is not None:
                center = "coord@399"
                site_coord = f"{self.observer_lon},{self.observer_lat},{self.observer_elev_km}"
            else:
                center = "500@399"  # geocentric (Earth center)
                site_coord = None

            params = {
                "format": "json",
                "COMMAND": f"'{self.body_id}'",
                "OBJ_DATA": "NO",
                "MAKE_EPHEM": "YES",
                "EPHEM_TYPE": "OBSERVER",
                "CENTER": f"'{center}'",
                "START_TIME": f"'{start}'",
                "STOP_TIME": f"'{stop}'",
                "STEP_SIZE": "'1m'",
                # QUANTITIES 1,2 -> astrometric RA/DEC and apparent RA/DEC
                "QUANTITIES": "'1,2'",
                "ANG_FORMAT": "'DEG'",
            }
            if site_coord:
                params["SITE_COORD"] = f"'{site_coord}'"
                params["COORD_TYPE"] = "'GEODETIC'"

            self.log_signal.emit(
                f"[HORIZONS] Querying NASA JPL Horizons for '{self.body_name}' (ID {self.body_id})..."
            )
            url = self.HORIZONS_URL + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={"User-Agent": "INDI-Telescope-Control/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")

            payload = json.loads(raw)
            result_text = payload.get("result", "")
            if not result_text:
                err = payload.get("error", "Empty response from Horizons.")
                self.error_signal.emit(f"Horizons error: {err}")
                return

            soe = result_text.find("$$SOE")
            eoe = result_text.find("$$EOE")
            if soe == -1 or eoe == -1:
                self.error_signal.emit(
                    f"Could not find ephemeris data block in Horizons response:\n{result_text[:500]}"
                )
                return

            block = result_text[soe + len("$$SOE"):eoe].strip()
            if not block:
                self.error_signal.emit("Horizons returned no ephemeris rows for this time.")
                return

            data_line = block.splitlines()[0].strip()

            # The row is comma-separated; pull out the numeric fields. With
            # QUANTITIES='1,2' the last two numeric columns are the apparent
            # (epoch-of-date) RA (deg) and DEC (deg), which is what we want
            # to feed straight into EQUATORIAL_EOD_COORD.
            fields = [f.strip() for f in data_line.split(",") if f.strip() != ""]
            numeric_fields = []
            for f in fields:
                try:
                    numeric_fields.append(float(f))
                except ValueError:
                    continue

            if len(numeric_fields) < 4:
                self.error_signal.emit(f"Unexpected Horizons data format:\n{data_line}")
                return

            ra_deg = numeric_fields[-2]
            dec_deg = numeric_fields[-1]
            ra_hours = ra_deg / 15.0

            self.result_signal.emit({
                "target_name": self.body_name.title(),
                "ra_hours": ra_hours,
                "dec_deg": dec_deg,
                "summary": (
                    f"Solar-system body resolved via NASA JPL Horizons "
                    f"(apparent geocentric RA/DEC, {now.strftime('%Y-%m-%d %H:%M UTC')})."
                ),
            })

        except Exception as e:
            self.error_signal.emit(f"Horizons lookup failed: {e}")



class GeminiWorker(QThread):
    result_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self, prompt, api_key, model_name):
        super().__init__()
        self.prompt = prompt
        self.api_key = api_key
        self.model_name = model_name

    def run(self):
        try:
            client = genai.Client(api_key=self.api_key)

            system_instruction = (
                "You are a target-classification and lookup engine for an equatorial telescope mount.\n"
                "For the user's free-text target description, first decide whether it refers to a "
                "SOLAR-SYSTEM BODY (Sun, Moon, a planet, a natural satellite/moon of a planet, an "
                "asteroid/minor planet, or a comet) or a FIXED BACKGROUND SKY OBJECT (star, star "
                "cluster, nebula, galaxy, quasar, etc).\n\n"
                "CASE A -- solar-system body:\n"
                "Do NOT provide RA/DEC yourself (those change minute to minute). Instead provide the "
                "exact NASA JPL Horizons COMMAND identifier for the body, in the same syntax Horizons' "
                "API expects, for example:\n"
                "  Sun -> \"10\", Mercury -> \"199\", Venus -> \"299\", Earth's Moon -> \"301\", "
                "Mars -> \"499\", Jupiter -> \"599\", a named Galilean moon -> its NAIF ID (e.g. "
                "Europa -> \"502\"), Saturn -> \"699\", Titan -> \"606\", Uranus -> \"799\", "
                "Neptune -> \"899\", Pluto -> \"999\".\n"
                "  Named minor planets/asteroids -> the small-body designation followed by a "
                "semicolon, e.g. Ceres -> \"1;\", Vesta -> \"4;\", Pallas -> \"2;\".\n"
                "  Named comets -> their small-body designation or SPK-ID, e.g. Halley -> \"90000030\".\n"
                "If you are not fully certain of the exact ID for an unusual or obscure body, give your "
                "best-supported answer rather than leaving it blank.\n\n"
                "CASE B -- fixed background object:\n"
                "Resolve its coordinates yourself: RA as decimal hours (0-24) and DEC as decimal "
                "degrees (-90 to +90), using standard J2000/current catalog coordinates.\n\n"
                "Output ONLY a single raw, valid JSON object (no markdown, no commentary) matching "
                "this schema:\n"
                "{\n"
                '  "is_solar_system_body": <true or false>,\n'
                '  "target_name": "Name of object",\n'
                '  "horizons_id": <Horizons COMMAND string as described above, or null if not a solar-system body>,\n'
                '  "ra_hours": <float 0.0-24.0, or null if this IS a solar-system body>,\n'
                '  "dec_deg": <float -90.0 to +90.0, or null if this IS a solar-system body>,\n'
                '  "summary": "1-sentence astronomical description"\n'
                "}"
            )

            response = client.models.generate_content(
                model=self.model_name,
                contents=self.prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json"
                )
            )

            raw_text = response.text
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                self.result_signal.emit(data)
            else:
                self.error_signal.emit(f"Could not parse JSON response from Gemini:\n{raw_text}")

        except Exception as e:
            self.error_signal.emit(str(e))


# -------------------------------------------------------------------------
# Voice Input Worker Thread (Speech -> Text)
# -------------------------------------------------------------------------
class VoiceWorker(QThread):

    log_signal = pyqtSignal(str)
    result_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, timeout=6, phrase_time_limit=8, language="en-US"):
        super().__init__()
        self.timeout = timeout
        self.phrase_time_limit = phrase_time_limit
        self.language = language

    def run(self):
        if not SPEECH_RECOGNITION_AVAILABLE:
            self.error_signal.emit(
                "The 'speech_recognition' package is not installed.\n"
                "Install it with: pip install SpeechRecognition PyAudio"
            )
            return

        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = True

        try:
            with sr.Microphone() as source:
                self.log_signal.emit("[VOICE] Calibrating for ambient noise...")
                recognizer.adjust_for_ambient_noise(source, duration=0.6)
                self.log_signal.emit("[VOICE] Listening... speak the target now.")
                audio = recognizer.listen(
                    source,
                    timeout=self.timeout,
                    phrase_time_limit=self.phrase_time_limit
                )
        except sr.WaitTimeoutError:
            self.error_signal.emit("No speech detected before timeout.")
            return
        except OSError as e:
            self.error_signal.emit(f"Microphone error: {e}")
            return
        except Exception as e:
            self.error_signal.emit(f"Unexpected error while listening: {e}")
            return

        self.log_signal.emit("[VOICE] Recognizing speech...")
        try:
            text = recognizer.recognize_google(audio, language=self.language)
            self.result_signal.emit(text)
        except sr.UnknownValueError:
            self.error_signal.emit("Could not understand the audio. Please try again.")
        except sr.RequestError as e:
            self.error_signal.emit(f"Speech recognition service error: {e}")
        except Exception as e:
            self.error_signal.emit(f"Unexpected recognition error: {e}")



# Main GUI Window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("INDI Telescope / LX200 Classic + Gemini 3.6 Flash + NASA Horizons")
        self.resize(820, 680)
        self.slew_worker = None
        self.init_ui()

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        #  Slew & AI Control
        ops_tab = QWidget()
        ops_layout = QVBoxLayout(ops_tab)

        # INDI Connection Config Box
        conn_box = QGroupBox("INDI Mount Connection")
        c_layout = QHBoxLayout(conn_box)

        self.combo_mount_type = QComboBox()
        self.combo_mount_type.addItems(list(MOUNT_DRIVERS.keys()))
        self.combo_mount_type.currentTextChanged.connect(self.on_mount_type_changed)

        self.txt_host = QLineEdit("localhost")
        self.txt_port = QLineEdit("7624")
        self.txt_device = QLineEdit(MOUNT_DRIVERS[self.combo_mount_type.currentText()])

        c_layout.addWidget(QLabel("Mount:")); c_layout.addWidget(self.combo_mount_type)
        c_layout.addWidget(QLabel("Host:")); c_layout.addWidget(self.txt_host)
        c_layout.addWidget(QLabel("Port:")); c_layout.addWidget(self.txt_port)
        c_layout.addWidget(QLabel("Device Name:")); c_layout.addWidget(self.txt_device)
        ops_layout.addWidget(conn_box)

        mount_hint = QLabel(

        )
        mount_hint.setStyleSheet("color: gray; font-style: italic;")
        mount_hint.setWordWrap(True)
        ops_layout.addWidget(mount_hint)

        # Observer Location Box
        loc_box = QGroupBox("Observer Location (optional — improves Sun/Moon/planet accuracy)")
        loc_layout = QHBoxLayout(loc_box)
        self.txt_lat = QLineEdit()
        self.txt_lat.setPlaceholderText("Latitude, e.g. 22.5967")
        self.txt_lon = QLineEdit()
        self.txt_lon.setPlaceholderText("Longitude, e.g. 75.6868")
        self.txt_elev = QLineEdit("0.0")
        loc_layout.addWidget(QLabel("Lat (°):")); loc_layout.addWidget(self.txt_lat)
        loc_layout.addWidget(QLabel("Lon (°):")); loc_layout.addWidget(self.txt_lon)
        loc_layout.addWidget(QLabel("Elev (km):")); loc_layout.addWidget(self.txt_elev)
        ops_layout.addWidget(loc_box)

        # Telemetry Box
        tel_box = QGroupBox("Mount State & Position")
        t_layout = QGridLayout(tel_box)
        self.lbl_status = QLabel("Idle / Ready")
        self.lbl_status.setStyleSheet("font-weight: bold; color: green;")
        self.lbl_ra = QLabel("--.---- h")
        self.lbl_dec = QLabel("--.---- °")
        t_layout.addWidget(QLabel("Mount State:"), 0, 0); t_layout.addWidget(self.lbl_status, 0, 1)
        t_layout.addWidget(QLabel("Current RA:"), 0, 2); t_layout.addWidget(self.lbl_ra, 0, 3)
        t_layout.addWidget(QLabel("Current DEC:"), 0, 4); t_layout.addWidget(self.lbl_dec, 0, 5)
        ops_layout.addWidget(tel_box)

        # AI / Ephemeris Prompt Box
        ai_box = QGroupBox("Target Lookup — Gemini 3.6 Flash (classify + resolve) + NASA Horizons (live solar-system positions)")
        ai_layout = QVBoxLayout(ai_box)
        prompt_h = QHBoxLayout()
        self.txt_prompt = QLineEdit()
        self.txt_prompt.setPlaceholderText(
            "e.g. 'point at the Moon', 'Center the Ring Nebula', 'Point at Sirius', 'slew to Saturn'"
        )
        self.txt_prompt.returnPressed.connect(self.resolve_target)
        self.btn_ask = QPushButton("Resolve Target")
        self.btn_ask.clicked.connect(self.resolve_target)

        self.btn_voice = QPushButton("🎤 Speak Target")
        self.btn_voice.setStyleSheet("background-color: #1565c0; color: white; font-weight: bold;")
        self.btn_voice.clicked.connect(self.start_voice_input)
        if not SPEECH_RECOGNITION_AVAILABLE:
            self.btn_voice.setToolTip(
                "Install with: pip install SpeechRecognition PyAudio"
            )

        prompt_h.addWidget(self.txt_prompt)
        prompt_h.addWidget(self.btn_voice)
        prompt_h.addWidget(self.btn_ask)
        ai_layout.addLayout(prompt_h)

        hint_lbl = QLabel(
            "Every prompt is first sent to Gemini, which decides whether it's a solar-system body "
            "(Sun, Moon, planet, moon, asteroid, comet) or a fixed background object (star, "
            "nebula, galaxy...). For solar-system bodies, Gemini supplies the NASA JPL Horizons "
            "ID on the fly (no local ID table — nothing is hardcoded or stored) and that ID is used "
            "to fetch a live position from Horizons. Fixed background objects are resolved by Gemini directly."
        )
        hint_lbl.setStyleSheet("color: gray; font-style: italic;")
        hint_lbl.setWordWrap(True)
        ai_layout.addWidget(hint_lbl)

        # Coordinates & Slew Buttons
        target_grid = QGridLayout()
        self.txt_target_name = QLineEdit(); self.txt_target_name.setReadOnly(True)
        self.txt_target_ra = QLineEdit("6.75")
        self.txt_target_dec = QLineEdit("-16.7")

        self.btn_slew = QPushButton("SLEW TO TARGET")
        self.btn_slew.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; height: 32px;")
        self.btn_slew.clicked.connect(self.start_slew)

        self.btn_abort = QPushButton("ABORT / STOP")
        self.btn_abort.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; height: 32px;")
        self.btn_abort.clicked.connect(self.abort_slew)

        target_grid.addWidget(QLabel("Target:"), 0, 0); target_grid.addWidget(self.txt_target_name, 0, 1)
        target_grid.addWidget(QLabel("Target RA (h):"), 0, 2); target_grid.addWidget(self.txt_target_ra, 0, 3)
        target_grid.addWidget(QLabel("Target DEC (°):"), 0, 4); target_grid.addWidget(self.txt_target_dec, 0, 5)
        target_grid.addWidget(self.btn_slew, 1, 4)
        target_grid.addWidget(self.btn_abort, 1, 5)
        ai_layout.addLayout(target_grid)
        ops_layout.addWidget(ai_box)

        # Logs Box
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        ops_layout.addWidget(QLabel("Operation Logs:"))
        ops_layout.addWidget(self.txt_log)
        self.tabs.addTab(ops_tab, "Telescope Control")

        #  Gemini Configuration
        key_tab = QWidget()
        k_layout = QVBoxLayout(key_tab)
        cfg_box = QGroupBox("Google Gemini Model Configuration")
        cfg_grid = QGridLayout(cfg_box)

        self.txt_api_key = QLineEdit()
        self.txt_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_api_key.setPlaceholderText("Paste your AI Studio API key here...")

        self.combo_model = QComboBox()
        self.combo_model.addItems([
            "gemini-3.6-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash"
        ])
        self.combo_model.setEditable(True)

        cfg_grid.addWidget(QLabel("API Key:"), 0, 0)
        cfg_grid.addWidget(self.txt_api_key, 0, 1)
        cfg_grid.addWidget(QLabel("Model ID:"), 1, 0)
        cfg_grid.addWidget(self.combo_model, 1, 1)
        k_layout.addWidget(cfg_box)

        info_lbl = QLabel(

        )
        info_lbl.setWordWrap(True)
        k_layout.addWidget(info_lbl)
        k_layout.addStretch()
        self.tabs.addTab(key_tab, "Gemini Settings")

    def log(self, text):
        self.txt_log.append(f"[{time.strftime('%H:%M:%S')}] {text}")

    def update_coords(self, ra, dec):
        self.lbl_ra.setText(f"{ra:.4f} h")
        self.lbl_dec.setText(f"{dec:.4f} °")

    def update_status(self, status):
        self.lbl_status.setText(status)
        color = "green" if "Idle" in status or "Connected" in status or "Tracking" in status else "orange"
        if "Error" in status or "ALERT" in status: color = "red"
        self.lbl_status.setStyleSheet(f"font-weight: bold; color: {color};")

    def on_mount_type_changed(self, label):

        device_name = MOUNT_DRIVERS.get(label)
        if device_name:
            self.txt_device.setText(device_name)
            self.log(f"[INFO] Mount type switched to '{label}' (device name: '{device_name}').")

    def start_voice_input(self):
        if not SPEECH_RECOGNITION_AVAILABLE:
            QMessageBox.warning(
                self,
                "Missing Dependency",
                "Voice input needs the 'speech_recognition' (and 'PyAudio') packages.\n\n"
                "Install them with:\n"
                "    pip install SpeechRecognition PyAudio"
            )
            return

        self.btn_voice.setEnabled(False)
        self.btn_voice.setText("🎤 Listening...")
        self.log("[VOICE] Starting microphone capture...")

        self.voice_thread = VoiceWorker()
        self.voice_thread.log_signal.connect(self.log)
        self.voice_thread.result_signal.connect(self.on_voice_result)
        self.voice_thread.error_signal.connect(self.on_voice_error)
        self.voice_thread.start()

    def on_voice_result(self, text):
        self.btn_voice.setEnabled(True)
        self.btn_voice.setText("🎤 Speak Target")
        self.log(f"[VOICE] Heard: \"{text}\"")
        self.txt_prompt.setText(text)
        # Automatically resolve the recognized phrase
        self.resolve_target()

    def on_voice_error(self, err):
        self.btn_voice.setEnabled(True)
        self.btn_voice.setText("🎤 Speak Target")
        self.log(f"[VOICE ERROR]: {err}")
        QMessageBox.warning(self, "Voice Input Error", err)

    def resolve_target(self):

        prompt = self.txt_prompt.text().strip()
        if not prompt:
            return
        self.ask_gemini(prompt)

    def ask_gemini(self, prompt=None):
        if prompt is None:
            prompt = self.txt_prompt.text().strip()
        if not prompt:
            return

        api_key = self.txt_api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "API Key Missing", "Paste your free Gemini API Key in the 'Gemini Settings' tab.")
            self.tabs.setCurrentIndex(1)
            return

        model_name = self.combo_model.currentText().strip()

        self.btn_ask.setEnabled(False)
        self.log(f"[USER -> Gemini ({model_name})]: {prompt}")

        self.gemini_thread = GeminiWorker(prompt, api_key, model_name)
        self.gemini_thread.result_signal.connect(self.on_gemini_classification)
        self.gemini_thread.error_signal.connect(self.on_gemini_error)
        self.gemini_thread.start()

    def on_gemini_classification(self, data):
  
        is_solar_system = bool(data.get("is_solar_system_body"))
        name = data.get("target_name", "Target")

        if is_solar_system:
            horizons_id = data.get("horizons_id")
            if not horizons_id:
                self.btn_ask.setEnabled(True)
                err = f"Gemini flagged '{name}' as a solar-system body but did not return a Horizons ID."
                self.log(f"[Resolve Error]: {err}")
                QMessageBox.critical(self, "Target Resolution Error", err)
                return
            self.log(f"[Gemini]: classified '{name}' as a solar-system body -> Horizons ID '{horizons_id}'")
            self.ask_horizons(name, str(horizons_id))
        else:
            self.log(f"[Gemini]: classified '{name}' as a fixed background object -> resolving directly.")
            self.on_gemini_result(data)

    def ask_horizons(self, body_name, body_id):
        lat = self.txt_lat.text().strip()
        lon = self.txt_lon.text().strip()
        elev = self.txt_elev.text().strip()

        observer_lat = observer_lon = None
        observer_elev_km = 0.0
        try:
            if lat and lon:
                observer_lat = float(lat)
                observer_lon = float(lon)
                observer_elev_km = float(elev) if elev else 0.0
        except ValueError:
            self.log("[WARN] Invalid observer location; falling back to geocentric coordinates.")
            observer_lat = observer_lon = None

        self.log(f"[USER -> NASA Horizons]: querying live position for '{body_name}' (ID {body_id})")

        self.horizons_thread = HorizonsWorker(body_name, body_id, observer_lat, observer_lon, observer_elev_km)
        self.horizons_thread.log_signal.connect(self.log)
        self.horizons_thread.result_signal.connect(self.on_gemini_result)
        self.horizons_thread.error_signal.connect(self.on_gemini_error)
        self.horizons_thread.start()

    def on_gemini_result(self, data):
        self.btn_ask.setEnabled(True)
        name = data.get("target_name", "Target")
        ra = float(data.get("ra_hours", 0.0))
        dec = float(data.get("dec_deg", 0.0))
        info = data.get("summary", "")

        self.txt_target_name.setText(name)
        self.txt_target_ra.setText(f"{ra:.4f}")
        self.txt_target_dec.setText(f"{dec:.4f}")
        self.log(f"[Resolved]: {name} -> RA: {ra:.4f}h, DEC: {dec:.4f}°")
        if info: self.log(f"[Info]: {info}")

    def on_gemini_error(self, err):
        self.btn_ask.setEnabled(True)
        self.log(f"[Resolve Error]: {err}")
        QMessageBox.critical(self, "Target Resolution Error", err)

    def start_slew(self):
        if self.slew_worker and self.slew_worker.isRunning():
            QMessageBox.warning(self, "Busy", "A slew is already in progress.")
            return

        try:
            ra = float(self.txt_target_ra.text())
            dec = float(self.txt_target_dec.text())
            if not (0.0 <= ra <= 24.0 and -90.0 <= dec <= 90.0):
                raise ValueError()
        except ValueError:
            QMessageBox.critical(self, "Invalid Coordinates", "RA must be between 0-24h and DEC between -90 and 90°.")
            return

        host = self.txt_host.text().strip()
        port = int(self.txt_port.text().strip())
        device = self.txt_device.text().strip()

        self.btn_slew.setEnabled(False)
        self.slew_worker = SlewWorker(host, port, device, ra, dec)
        self.slew_worker.log_signal.connect(self.log)
        self.slew_worker.status_signal.connect(self.update_status)
        self.slew_worker.coords_signal.connect(self.update_coords)
        self.slew_worker.finished_signal.connect(self.on_slew_done)
        self.slew_worker.start()

    def abort_slew(self):
        if self.slew_worker and self.slew_worker.isRunning():
            self.slew_worker.abort()

    def on_slew_done(self, success, message):
        self.btn_slew.setEnabled(True)
        self.log(f"[{'OK' if success else 'ALERT'}]: {message}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
