__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"


import collections
import datetime
import math
import os
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional
import asyncio
from pybambu import BambuClient, commands
import logging
import logging.handlers

from serial import SerialTimeoutException
from octoprint.util import RepeatedTimer, to_bytes, to_unicode, get_dos_filename
from octoprint.util.files import unix_timestamp_to_m20_timestamp

from octoprint_bambu_printer.gcode_executor import GCodeExecutor

from .char_counting_queue import CharCountingQueue
from .ftpsclient import IoTFTPSClient


# noinspection PyBroadException
class BambuVirtualPrinter:
    gcode_executor = GCodeExecutor()
    command_regex = re.compile(r"^([GM])(\d+)")

    def __init__(
        self,
        settings,
        printer_profile_manager,
        data_folder,
        seriallog_handler=None,
        read_timeout=5.0,
        write_timeout=10.0,
        faked_baudrate=115200,
    ):
        self._busyInterval = 2.0
        self.tick_rate = 2.0
        self._errors = {
            "checksum_mismatch": "Checksum mismatch",
            "checksum_missing": "Missing checksum",
            "lineno_mismatch": "expected line {} got {}",
            "lineno_missing": "No Line Number with checksum, Last Line: {}",
            "maxtemp": "MAXTEMP triggered!",
            "mintemp": "MINTEMP triggered!",
            "command_unknown": "Unknown command {}",
        }
        self._sendBusy = False
        self._ambient_temperature = 21.3
        self.temp = [self._ambient_temperature]
        self.targetTemp = [0.0]
        self.bedTemp = self._ambient_temperature
        self.bedTargetTemp = 0.0
        self._hasChamber = printer_profile_manager.get_current().get("heatedChamber")
        self.chamberTemp = self._ambient_temperature
        self.chamberTargetTemp = 0.0
        self.lastTempAt = time.monotonic()
        self._firmwareName = "Bambu"
        self._m115FormatString = "FIRMWARE_NAME:{firmware_name} PROTOCOL_VERSION:1.0"
        self._received_lines = 0
        self.extruderCount = 1
        self._waitInterval = 5.0
        self._killed = False
        self._heatingUp = False
        self.current_line = 0
        self._writingToSd = False

        self._sdPrinter = None
        self._sdPrinting = False
        self._sdPrintStarting = False
        self._sdPrintingSemaphore = threading.Event()
        self._sdPrintingPausedSemaphore = threading.Event()
        self._sdFileListCache = {}
        self._selectedSdFile = None
        self._selectedSdFileSize = 0
        self._selectedSdFilePos = 0

        self._busy = None
        self._busy_loop = None

        self._logger = logging.getLogger("octoprint.plugins.bambu_printer.BambuPrinter")

        self._settings = settings
        self._printer_profile_manager = printer_profile_manager
        self._faked_baudrate = faked_baudrate
        self._plugin_data_folder = data_folder

        self._serial_log = logging.getLogger(
            "octoprint.plugins.bambu_printer.BambuPrinter.serial"
        )
        self._serial_log.setLevel(logging.CRITICAL)
        self._serial_log.propagate = False

        if seriallog_handler is not None:
            self._serial_log.addHandler(seriallog_handler)
            self._serial_log.setLevel(logging.INFO)

        self._serial_log.debug("-" * 78)

        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

        self._rx_buffer_size = 64
        self._incoming_lock = threading.RLock()

        self.incoming = CharCountingQueue(self._rx_buffer_size, name="RxBuffer")
        self.outgoing = queue.Queue()
        self.buffered = queue.Queue(maxsize=4)

        self._last_hms_errors = None

        self._bambu: BambuClient = None

        readThread = threading.Thread(
            target=self._processIncoming,
            name="octoprint.plugins.bambu_printer.wait_thread",
            daemon=True,
        )
        readThread.start()

        # bufferThread = threading.Thread(
        #     target=self._processBuffer,
        #     name="octoprint.plugins.bambu_printer.buffer_thread",
        #     daemon=True
        # )
        # bufferThread.start()

        # Move this into M110 command response?
        connectionThread = threading.Thread(
            target=self._create_connection,
            name="octoprint.plugins.bambu_printer.connection_thread",
            daemon=True,
        )
        connectionThread.start()

    @property
    def bambu(self):
        if self._bambu is None:
            raise ValueError("No connection to Bambulab was established")
        return self._bambu

    def new_update(self, event_type):
        if event_type == "event_hms_errors":
            bambu_printer = self.bambu.get_device()
            if (
                bambu_printer.hms.errors != self._last_hms_errors
                and bambu_printer.hms.errors["Count"] > 0
            ):
                self._logger.debug(f"HMS Error: {bambu_printer.hms.errors}")
                for n in range(1, bambu_printer.hms.errors["Count"] + 1):
                    error = bambu_printer.hms.errors[f"{n}-Error"].strip()
                    self._send(f"// action:notification {error}")
                self._last_hms_errors = bambu_printer.hms.errors
        elif event_type == "event_printer_data_update":
            device_data = self.bambu.get_device()
            ams = device_data.ams.__dict__
            print_job = device_data.print_job.__dict__
            temperatures = device_data.temperature.__dict__
            lights = device_data.lights.__dict__
            fans = device_data.fans.__dict__
            speed = device_data.speed.__dict__

            # self._logger.debug(device_data)

            self.lastTempAt = time.monotonic()
            self.temp[0] = temperatures.get("nozzle_temp", 0.0)
            self.targetTemp[0] = temperatures.get("target_nozzle_temp", 0.0)
            self.bedTemp = temperatures.get("bed_temp", 0.0)
            self.bedTargetTemp = temperatures.get("target_bed_temp", 0.0)
            self.chamberTemp = temperatures.get("chamber_temp", 0.0)

            if print_job.get("gcode_state") == "RUNNING":
                if not self._sdPrintingSemaphore.is_set():
                    self._sdPrintingSemaphore.set()
                if self._sdPrintingPausedSemaphore.is_set():
                    self._sdPrintingPausedSemaphore.clear()
                self._sdPrintStarting = False
                if not self._sdPrinting:
                    filename: str = print_job.get("subtask_name")
                    if not self._sdFileListCache.get(filename.lower()):
                        if self._sdFileListCache.get(f"{filename.lower()}.3mf"):
                            filename = f"{filename.lower()}.3mf"
                        elif self._sdFileListCache.get(f"{filename.lower()}.gcode.3mf"):
                            filename = f"{filename.lower()}.gcode.3mf"
                        elif filename.startswith("cache/"):
                            filename = filename[6:]
                        else:
                            self._logger.debug(f"No 3mf file found for {print_job}")

                    self._selectSdFile(filename)
                    self._startSdPrint(from_printer=True)

                # fuzzy math here to get print percentage to match BambuStudio
                self._selectedSdFilePos = int(
                    self._selectedSdFileSize
                    * ((print_job.get("print_percentage") + 1) / 100)
                )

            if print_job.get("gcode_state") == "PAUSE":
                if not self._sdPrintingPausedSemaphore.is_set():
                    self._sdPrintingPausedSemaphore.set()
                if self._sdPrintingSemaphore.is_set():
                    self._sdPrintingSemaphore.clear()
                    self._send("// action:paused")
                    self._sendPaused()

            if (
                print_job.get("gcode_state") == "FINISH"
                or print_job.get("gcode_state") == "FAILED"
            ):
                if self._sdPrintStarting is False:
                    self._sdPrinting = False
                if self._sdPrintingSemaphore.is_set():
                    self._selectedSdFilePos = self._selectedSdFileSize
                    self._finishSdPrint()

    def _create_connection(self):
        if (
            self._settings.get(["device_type"]) != ""
            and self._settings.get(["serial"]) != ""
            and self._settings.get(["serial"]) != ""
            and self._settings.get(["username"]) != ""
            and self._settings.get(["access_code"]) != ""
        ):
            asyncio.run(self._create_connection_async())

    def on_disconnect(self, on_disconnect):
        self._logger.debug(f"on disconnect called")
        return on_disconnect

    def on_connect(self, on_connect):
        self._logger.debug(f"on connect called")
        return on_connect

    async def _create_connection_async(self):
        self._logger.debug(
            f"connecting via local mqtt: {self._settings.get_boolean(['local_mqtt'])}"
        )
        self._bambu = BambuClient(
            device_type=self._settings.get(["device_type"]),
            serial=self._settings.get(["serial"]),
            host=self._settings.get(["host"]),
            username=(
                "bblp"
                if self._settings.get_boolean(["local_mqtt"])
                else self._settings.get(["username"])
            ),
            access_code=self._settings.get(["access_code"]),
            local_mqtt=self._settings.get_boolean(["local_mqtt"]),
            region=self._settings.get(["region"]),
            email=self._settings.get(["email"]),
            auth_token=self._settings.get(["auth_token"]),
        )
        self._bambu.on_disconnect = self.on_disconnect(self._bambu.on_disconnect)
        self._bambu.on_connect = self.on_connect(self._bambu.on_connect)
        self._bambu.connect(callback=self.new_update)
        self._logger.info(f"bambu connection status: {self._bambu.connected}")
        self._sendOk()

    def __str__(self):
        return "BAMBU(read_timeout={read_timeout},write_timeout={write_timeout},options={options})".format(
            read_timeout=self._read_timeout,
            write_timeout=self._write_timeout,
            options={
                "device_type": self._settings.get(["device_type"]),
                "host": self._settings.get(["host"]),
            },
        )

    def _calculate_resend_every_n(self, resend_ratio):
        self._resend_every_n = (100 // resend_ratio) if resend_ratio else 0

    def _reset(self):
        with self._incoming_lock:
            self._relative = True
            self._lastX = 0.0
            self._lastY = 0.0
            self._lastZ = 0.0
            self._lastE = [0.0] * self.extruderCount
            self._lastF = 200

            self._unitModifier = 1
            self._feedrate_multiplier = 100
            self._flowrate_multiplier = 100

            self._sdPrinting = False
            self._sdPrintStarting = False
            if self._sdPrinter:
                self._sdPrinting = False
                self._sdPrintingSemaphore.clear()
                self._sdPrintingPausedSemaphore.clear()
            self._sdPrinter = None
            self._selectedSdFile = None
            self._selectedSdFileSize = None
            self._selectedSdFilePos = None

            if self._writingToSdHandle:
                try:
                    self._writingToSdHandle.close()
                except Exception:
                    pass
            self._writingToSd = False
            self._writingToSdHandle = None
            self._writingToSdFile = None
            self._newSdFilePos = None

            self._heatingUp = False

            self.current_line = 0
            self.lastN = 0

            self._debug_awol = False
            self._debug_sleep = 0
            # self._sleepAfterNext.clear()
            # self._sleepAfter.clear()

            self._dont_answer = False
            self._broken_klipper_connection = False

            self._debug_drop_connection = False

            self._killed = False

            if self._sdstatus_reporter is not None:
                self._sdstatus_reporter.cancel()
                self._sdstatus_reporter = None

            self._clearQueue(self.incoming)
            self._clearQueue(self.outgoing)
            # self._clearQueue(self.buffered)

            if self._settings.get_boolean(["simulateReset"]):
                for item in self._settings.get(["resetLines"]):
                    self._send(item + "\n")

            self._locked = self._settings.get_boolean(["locked"])

    @property
    def timeout(self):
        return self._read_timeout

    @timeout.setter
    def timeout(self, value):
        self._logger.debug(f"Setting read timeout to {value}s")
        self._read_timeout = value

    @property
    def write_timeout(self):
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value):
        self._logger.debug(f"Setting write timeout to {value}s")
        self._write_timeout = value

    @property
    def port(self):
        return "BAMBU"

    @property
    def baudrate(self):
        return self._faked_baudrate

    # noinspection PyMethodMayBeStatic
    def _clearQueue(self, q):
        try:
            while q.get(block=False):
                q.task_done()
                continue
        except queue.Empty:
            pass

    def _processIncoming(self):
        linenumber = 0
        next_wait_timeout = 0

        def recalculate_next_wait_timeout():
            nonlocal next_wait_timeout
            next_wait_timeout = time.monotonic() + self._waitInterval

        recalculate_next_wait_timeout()

        data = None

        buf = b""
        while self.incoming is not None and not self._killed:
            try:
                data = self.incoming.get(timeout=0.01)
                data = to_bytes(data, encoding="ascii", errors="replace")
                self.incoming.task_done()
            except queue.Empty:
                continue
            except Exception:
                if self.incoming is None:
                    # just got closed
                    break

            if data is not None:
                buf += data
                nl = buf.find(b"\n") + 1
                if nl > 0:
                    data = buf[:nl]
                    buf = buf[nl:]
                else:
                    continue

            recalculate_next_wait_timeout()

            if data is None:
                continue

            self._received_lines += 1

            # strip checksum
            if b"*" in data:
                checksum = int(data[data.rfind(b"*") + 1 :])
                data = data[: data.rfind(b"*")]
                if not checksum == self._calculate_checksum(data):
                    self._triggerResend(expected=self.current_line + 1)
                    continue

                self.current_line += 1
            elif self._settings.get_boolean(["forceChecksum"]):
                self._send(self._error("checksum_missing"))
                continue

            # track N = N + 1
            if data.startswith(b"N") and b"M110" in data:
                linenumber = int(re.search(b"N([0-9]+)", data).group(1))
                self.lastN = linenumber
                self.current_line = linenumber
                self._sendOk()
                continue

            elif data.startswith(b"N"):
                linenumber = int(re.search(b"N([0-9]+)", data).group(1))
                expected = self.lastN + 1
                if linenumber != expected:
                    self._triggerResend(actual=linenumber)
                    continue
                else:
                    self.lastN = linenumber

                data = data.split(None, 1)[1].strip()

            data += b"\n"

            data = to_unicode(data, encoding="ascii", errors="replace").strip()

            # actual command handling
            command_match = BambuVirtualPrinter.command_regex.match(data)
            if command_match is not None:
                command = command_match.group(0)
                letter = command_match.group(1)

                if letter in self.gcode_executor:
                    handled = self.run_gcode_handler(letter, data)
                else:
                    handled = self.run_gcode_handler(command, data)
                if handled:
                    self._sendOk()
                    continue

                if self.bambu.connected:
                    GCODE_COMMAND = commands.SEND_GCODE_TEMPLATE
                    GCODE_COMMAND["print"]["param"] = data + "\n"
                    if self.bambu.publish(GCODE_COMMAND):
                        self._logger.info("command sent successfully")
                        self._sendOk()
                        continue
                self._logger.debug(f"{data}")

            self._logger.debug("Closing down read loop")

    ##~~ command implementations
    def run_gcode_handler(self, gcode, data):
        self.gcode_executor.execute(self, gcode, data)

    @gcode_executor.register("M21")
    def _gcode_M21(self, data: str) -> bool:
        self._send("SD card ok")
        return True

    @gcode_executor.register("M23")
    def _gcode_M23(self, data: str) -> bool:
        filename = data.split(maxsplit=1)[1].strip()
        self._selectSdFile(filename)
        return True

    @gcode_executor.register("M26")
    def _gcode_M26(self, data: str) -> bool:
        if data == "M26 S0":
            return self._cancelSdPrint()
        else:
            self._logger.debug("ignoring M26 command.")
            self._send("M26 disabled for Bambu")
            return True

    @gcode_executor.register("M27")
    def _gcode_M27(self, data: str) -> bool:
        matchS = re.search(r"S([0-9]+)", data)
        if matchS:
            interval = int(matchS.group(1))
            if self._sdstatus_reporter is not None:
                self._sdstatus_reporter.cancel()

            if interval > 0:
                self._sdstatus_reporter = RepeatedTimer(interval, self._reportSdStatus)
                self._sdstatus_reporter.start()
            else:
                self._sdstatus_reporter = None

        self._reportSdStatus()
        return True

    @gcode_executor.register("M30")
    def _gcode_M30(self, data: str) -> bool:
        filename = data.split(None, 1)[1].strip()
        self._deleteSdFile(filename)
        return True

    @gcode_executor.register("M105")
    def _gcode_M105(self, data: str) -> bool:
        return self._processTemperatureQuery()

    # noinspection PyUnusedLocal
    @gcode_executor.register("M115")
    def _gcode_M115(self, data: str) -> bool:
        self._send("Bambu Printer Integration")
        self._send("Cap:EXTENDED_M20:1")
        self._send("Cap:LFN_WRITE:1")
        self._send("Cap:LFN_WRITE:1")
        return True

    @gcode_executor.register("M117")
    def _gcode_M117(self, data: str) -> bool:
        # we'll just use this to echo a message, to allow playing around with pause triggers
        result = re.search(r"M117\s+(.*)", data).group(1)
        self._send(f"echo:{result}")
        return False

    @gcode_executor.register("M118")
    def _gcode_M118(self, data: str) -> bool:
        match = re.search(r"M118 (?:(?P<parameter>A1|E1|Pn[012])\s)?(?P<text>.*)", data)
        if not match:
            self._send("Unrecognized command parameters for M118")
        else:
            result = match.groupdict()
            text = result["text"]
            parameter = result["parameter"]

            if parameter == "A1":
                self._send(f"//{text}")
            elif parameter == "E1":
                self._send(f"echo:{text}")
            else:
                self._send(text)
        return True

    # noinspection PyUnusedLocal
    @gcode_executor.register("M220")
    def _gcode_M220(self, data: str) -> bool:
        if self.bambu.connected:
            gcode_command = commands.SEND_GCODE_TEMPLATE
            percent = int(data[1:])

            if percent is None or percent < 1 or percent > 166:
                return True

            speed_fraction = 100 / percent
            acceleration = math.exp((speed_fraction - 1.0191) / -0.814)
            feed_rate = (
                2.1645 * (acceleration**3)
                - 5.3247 * (acceleration**2)
                + 4.342 * acceleration
                - 0.181
            )
            speed_level = 1.539 * (acceleration**2) - 0.7032 * acceleration + 4.0834
            speed_command = f"M204.2 K${acceleration:.2f} \nM220 K${feed_rate:.2f} \nM73.2 R${speed_fraction:.2f} \nM1002 set_gcode_claim_speed_level ${speed_level:.0f}\n"

            gcode_command["print"]["param"] = speed_command
            if self.bambu.publish(gcode_command):
                self._logger.info(
                    f"{percent}% speed adjustment command sent successfully"
                )
        return True

    @staticmethod
    def _check_param_letters(letters, data):
        # Checks if any of the params (letters) are included in data
        # Purely for saving typing :)
        for param in list(letters):
            if param in data:
                return True

    ##~~ further helpers

    # noinspection PyMethodMayBeStatic
    def _calculate_checksum(self, line: bytes) -> int:
        checksum = 0
        for c in bytearray(line):
            checksum ^= c
        return checksum

    def _kill(self):
        self._killed = True
        if self.bambu.connected:
            self.bambu.disconnect()
        self._send("echo:EMERGENCY SHUTDOWN DETECTED. KILLED.")

    def _triggerResend(
        self, expected: int = None, actual: int = None, checksum: int = None
    ) -> None:
        with self._incoming_lock:
            if expected is None:
                expected = self.lastN + 1
            else:
                self.lastN = expected - 1

            if actual is None:
                if checksum:
                    self._send(self._error("checksum_mismatch"))
                else:
                    self._send(self._error("checksum_missing"))
            else:
                self._send(self._error("lineno_mismatch", expected, actual))

            def request_resend():
                self._send("Resend:%d" % expected)
                # if not self._brokenResend:
                self._sendOk()

            request_resend()

    @gcode_executor.register_no_data("M20")
    def _listSd(self):
        line = '{dosname} {size} {timestamp} "{name}"'

        self._send("Begin file list")
        for item in map(lambda x: line.format(**x), self._getSdFiles()):
            self._send(item)
        self._send("End file list")

    def _mappedSdList(self) -> Dict[str, Dict[str, Any]]:
        result = {}
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])

        ftp = IoTFTPSClient(f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True)
        filelist = ftp.list_files("", ".3mf") or []

        for entry in filelist:
            if entry.startswith("/"):
                filename = entry[1:]
            else:
                filename = entry
            filesize = ftp.ftps_session.size(entry)
            date_str = ftp.ftps_session.sendcmd(f"MDTM {entry}").replace("213 ", "")
            filedate = (
                datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S")
                .replace(tzinfo=datetime.timezone.utc)
                .timestamp()
            )
            dosname = get_dos_filename(
                filename, existing_filenames=list(result.keys())
            ).lower()
            data = {
                "dosname": dosname,
                "name": filename,
                "path": filename,
                "size": filesize,
                "timestamp": unix_timestamp_to_m20_timestamp(int(filedate)),
            }
            result[dosname.lower()] = filename.lower()
            result[filename.lower()] = data

        filelistcache = ftp.list_files("cache/", ".3mf") or []

        for entry in filelistcache:
            if entry.startswith("/"):
                filename = entry[1:].replace("cache/", "")
            else:
                filename = entry.replace("cache/", "")
            filesize = ftp.ftps_session.size(f"cache/{filename}")
            date_str = ftp.ftps_session.sendcmd(f"MDTM cache/{filename}").replace(
                "213 ", ""
            )
            filedate = (
                datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S")
                .replace(tzinfo=datetime.timezone.utc)
                .timestamp()
            )
            dosname = get_dos_filename(
                filename, existing_filenames=list(result.keys())
            ).lower()
            data = {
                "dosname": dosname,
                "name": filename,
                "path": "cache/" + filename,
                "size": filesize,
                "timestamp": unix_timestamp_to_m20_timestamp(int(filedate)),
            }
            result[dosname.lower()] = filename.lower()
            result[filename.lower()] = data

        return result

    def _getSdFileData(self, filename: str) -> Optional[Dict[str, Any]]:
        self._logger.debug(f"_getSdFileData: {filename}")
        data = self._sdFileListCache.get(filename.lower())
        if isinstance(data, str):
            data = self._sdFileListCache.get(data.lower())
        self._logger.debug(f"_getSdFileData: {data}")
        return data

    def _getSdFiles(self) -> List[Dict[str, Any]]:
        self._sdFileListCache = self._mappedSdList()
        self._logger.debug(f"_getSdFiles return: {self._sdFileListCache}")
        return [x for x in self._sdFileListCache.values() if isinstance(x, dict)]

    def _selectSdFile(self, filename: str, check_already_open: bool = False) -> None:
        self._logger.debug(
            f"_selectSdFile: {filename}, check_already_open={check_already_open}"
        )
        if filename.startswith("/"):
            filename = filename[1:]

        file = self._getSdFileData(filename)
        if file is None:
            self._listSd()
            self._sendOk()
            file = self._getSdFileData(filename)
            if file is None:
                self._send(f"{filename} open failed")
                return

        if self._selectedSdFile == file["path"] and check_already_open:
            return

        self._selectedSdFile = file["path"]
        self._selectedSdFileSize = file["size"]
        self._send(f"File opened: {file['name']}  Size: {self._selectedSdFileSize}")
        self._send("File selected")

    @gcode_executor.register_no_data("M24")
    def _startSdPrint(self, from_printer: bool = False) -> bool:
        self._logger.debug(f"_startSdPrint: from_printer={from_printer}")
        if self._selectedSdFile is not None:
            if self._sdPrinter is None:
                self._sdPrinting = True
                self._sdPrintStarting = True
                self._sdPrinter = threading.Thread(
                    target=self._sdPrintingWorker, kwargs={"from_printer": from_printer}
                )
                self._sdPrinter.start()

        if self._sdPrinter is not None:
            if self.bambu.connected:
                if self.bambu.publish(commands.RESUME):
                    self._logger.info("print resumed")
                else:
                    self._logger.info("print resume failed")
        return True

    @gcode_executor.register_no_data("M25")
    def _pauseSdPrint(self):
        if self.bambu.connected:
            if self.bambu.publish(commands.PAUSE):
                self._logger.info("print paused")
            else:
                self._logger.info("print pause failed")

    @gcode_executor.register("M524")
    def _cancelSdPrint(self) -> bool:
        if self.bambu.connected:
            if self.bambu.publish(commands.STOP):
                self._logger.info("print cancelled")
                self._finishSdPrint()
                return True
            else:
                self._logger.info("print cancel failed")
                return False
        return False

    def _setSdPos(self, pos):
        self._newSdFilePos = pos

    def _reportSdStatus(self):
        if (
            self._sdPrinter is not None or self._sdPrintStarting is True
        ) and self._selectedSdFileSize > 0:
            self._send(
                f"SD printing byte {self._selectedSdFilePos}/{self._selectedSdFileSize}"
            )
        else:
            self._send("Not SD printing")

    def _generateTemperatureOutput(self) -> str:
        template = "{heater}:{actual:.2f}/ {target:.2f}"
        temps = collections.OrderedDict()
        temps["T"] = (self.temp[0], self.targetTemp[0])
        temps["B"] = (self.bedTemp, self.bedTargetTemp)
        if self._hasChamber:
            temps["C"] = (self.chamberTemp, self.chamberTargetTemp)

        output = " ".join(
            map(
                lambda x: template.format(heater=x[0], actual=x[1][0], target=x[1][1]),
                temps.items(),
            )
        )
        output += " @:64\n"
        return output

    def _processTemperatureQuery(self) -> bool:
        # includeOk = not self._okBeforeCommandOutput
        if self.bambu.connected:
            output = self._generateTemperatureOutput()
            self._send(output)
            return True
        else:
            return False

    def _writeSdFile(self, filename: str) -> None:
        self._send(f"Writing to file: {filename}")

    def _finishSdFile(self):
        try:
            self._writingToSdHandle.close()
        except Exception:
            pass
        finally:
            self._writingToSdHandle = None
        self._writingToSd = False
        self._selectedSdFile = None
        # Most printers don't have RTC and set some ancient date
        # by default. Emulate that using 2000-01-01 01:00:00
        # (taken from prusa firmware behaviour)
        st = os.stat(self._writingToSdFile)
        os.utime(self._writingToSdFile, (st.st_atime, 946684800))
        self._writingToSdFile = None
        self._send("Done saving file")

    def _sdPrintingWorker(self, from_printer: bool = False):
        self._selectedSdFilePos = 0
        try:
            if not from_printer and self.bambu.connected:
                print_command = {
                    "print": {
                        "sequence_id": 0,
                        "command": "project_file",
                        "param": "Metadata/plate_1.gcode",
                        "md5": "",
                        "profile_id": "0",
                        "project_id": "0",
                        "subtask_id": "0",
                        "task_id": "0",
                        "subtask_name": f"{self._selectedSdFile}",
                        "file": f"{self._selectedSdFile}",
                        "url": (
                            f"file:///mnt/sdcard/{self._selectedSdFile}"
                            if self._settings.get_boolean(["device_type"])
                            in ["X1", "X1C"]
                            else f"file:///sdcard/{self._selectedSdFile}"
                        ),
                        "timelapse": self._settings.get_boolean(["timelapse"]),
                        "bed_leveling": self._settings.get_boolean(["bed_leveling"]),
                        "flow_cali": self._settings.get_boolean(["flow_cali"]),
                        "vibration_cali": self._settings.get_boolean(
                            ["vibration_cali"]
                        ),
                        "layer_inspect": self._settings.get_boolean(["layer_inspect"]),
                        "use_ams": self._settings.get_boolean(["use_ams"]),
                    }
                }
                self.bambu.publish(print_command)

            while self._selectedSdFilePos < self._selectedSdFileSize:
                if self._killed or not self._sdPrinting:
                    break

                # if we are paused, wait for resuming
                self._sdPrintingSemaphore.wait()
                self._reportSdStatus()
                time.sleep(3)
            self._logger.debug(f"SD File Print: {self._selectedSdFile}")
        except AttributeError:
            if self.outgoing is not None:
                raise

        self._finishSdPrint()

    def _finishSdPrint(self):
        if not self._killed:
            self._sdPrintingSemaphore.clear()
            self._sdPrintingPausedSemaphore.clear()
            self._send("Done printing file")
            self._selectedSdFilePos = 0
            self._selectedSdFileSize = 0
            self._sdPrinting = False
            self._sdPrintStarting = False
            self._sdPrinter = None

    def _deleteSdFile(self, filename: str) -> None:
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])

        if filename.startswith("/"):
            filename = filename[1:]
        file = self._getSdFileData(filename)
        if file is not None:
            ftp = IoTFTPSClient(
                f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
            )
            try:
                if ftp.delete_file(file["path"]):
                    self._logger.debug(f"{filename} deleted")
                else:
                    raise Exception("delete failed")
            except Exception as e:
                self._logger.debug(f"Error deleting file {filename}")

    def _setBusy(self, reason="processing"):
        if not self._sendBusy:
            return

        def loop():
            while self._busy:
                self._send(f"echo:busy {self._busy}")
                time.sleep(self._busyInterval)
            self._sendOk()

        self._busy = reason
        self._busy_loop = threading.Thread(target=loop)
        self._busy_loop.daemon = True
        self._busy_loop.start()

    def _setUnbusy(self):
        self._busy = None

    def _showPrompt(self, text, choices):
        self._hidePrompt()
        self._send(f"//action:prompt_begin {text}")
        for choice in choices:
            self._send(f"//action:prompt_button {choice}")
        self._send("//action:prompt_show")

    def _hidePrompt(self):
        self._send("//action:prompt_end")

    def write(self, data: bytes) -> int:
        data = to_bytes(data, errors="replace")
        u_data = to_unicode(data, errors="replace")

        with self._incoming_lock:
            if self.incoming is None or self.outgoing is None:
                return 0

            if b"M112" in data:
                self._serial_log.debug(f"<<< {u_data}")
                self._kill()
                return len(data)

            try:
                written = self.incoming.put(
                    data, timeout=self._write_timeout, partial=True
                )
                self._serial_log.debug(f"<<< {u_data}")
                return written
            except queue.Full:
                self._logger.info(
                    "Incoming queue is full, raising SerialTimeoutException"
                )
                raise SerialTimeoutException()

    def readline(self) -> bytes:
        assert self.outgoing is not None
        timeout = self._read_timeout

        try:
            # fetch a line from the queue, wait no longer than timeout
            line = to_unicode(self.outgoing.get(timeout=timeout), errors="replace")
            self._serial_log.debug(f">>> {line.strip()}")
            self.outgoing.task_done()
            return to_bytes(line)
        except queue.Empty:
            # queue empty? return empty line
            return b""

    def close(self):
        if self.bambu.connected:
            self.bambu.disconnect()
        self._killed = True
        self.incoming = None
        self.outgoing = None
        self.buffered = None

    def _sendOk(self):
        if self.outgoing is None:
            return
        self._send("ok")

    def _isPaused(self):
        return self._sdPrintingPausedSemaphore.is_set()

    def _sendPaused(self):
        paused_timer = RepeatedTimer(
            interval=3.0,
            function=self._send,
            args=[
                f"SD printing byte {self._selectedSdFilePos}/{self._selectedSdFileSize}"
            ],
            daemon=True,
            run_first=True,
            condition=self._isPaused,
        )
        paused_timer.start()

    def _send(self, line: str) -> None:
        if self.outgoing is not None:
            self.outgoing.put(line)

    def _error(self, error: str, *args, **kwargs) -> str:
        return f"Error: {self._errors.get(error).format(*args, **kwargs)}"
