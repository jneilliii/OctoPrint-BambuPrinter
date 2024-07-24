__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"


import collections
from dataclasses import dataclass, field
import math
import os
import queue
import re
import threading
import time
import asyncio
from pybambu import BambuClient, commands
import logging
import logging.handlers

from octoprint.util import RepeatedTimer

from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState
from octoprint_bambu_printer.printer.states.idle_state import IdleState

from .printer_serial_io import PrinterSerialIO
from .states.print_finished_state import PrintFinishedState
from .states.paused_state import PausedState
from .states.printing_state import PrintingState

from .gcode_executor import GCodeExecutor
from .remote_sd_card_file_list import RemoteSDCardFileList


AMBIENT_TEMPERATURE: float = 21.3


@dataclass
class BambuPrinterTelemetry:
    temp: list[float] = field(default_factory=lambda: [AMBIENT_TEMPERATURE])
    targetTemp: list[float] = field(default_factory=lambda: [0.0])
    bedTemp: float = AMBIENT_TEMPERATURE
    bedTargetTemp = 0.0
    hasChamber: bool = False
    chamberTemp: float = AMBIENT_TEMPERATURE
    chamberTargetTemp: float = 0.0
    lastTempAt: float = time.monotonic()
    firmwareName: str = "Bambu"
    extruderCount: int = 1


# noinspection PyBroadException
class BambuVirtualPrinter:
    gcode_executor = GCodeExecutor()

    def __init__(
        self,
        settings,
        printer_profile_manager,
        data_folder,
        serial_log_handler=None,
        read_timeout=5.0,
        faked_baudrate=115200,
    ):
        self._log = logging.getLogger("octoprint.plugins.bambu_printer.BambuPrinter")

        self._state_idle = IdleState(self)
        self._state_printing = PrintingState(self)
        self._state_paused = PausedState(self)
        self._state_finished = PrintFinishedState(self)
        self._current_state = self._state_idle
        self._serial_io = PrinterSerialIO(
            handle_command_callback=self._process_gcode_serial_command,
            settings=settings,
            serial_log_handler=serial_log_handler,
            read_timeout=read_timeout,
            write_timeout=10.0,
        )

        self._telemetry = BambuPrinterTelemetry()
        self._telemetry.hasChamber = printer_profile_manager.get_current().get(
            "heatedChamber"
        )

        self._running = True
        self.file_system = RemoteSDCardFileList(settings)

        self._busy_reason = None
        self._busy_loop = None
        self._busy_interval = 2.0

        self._settings = settings
        self._printer_profile_manager = printer_profile_manager
        self._faked_baudrate = faked_baudrate
        self._plugin_data_folder = data_folder

        self._last_hms_errors = None

        self._serial_io.start()

        self._bambu_client: BambuClient = None
        asyncio.get_event_loop().run_until_complete(self._create_connection_async())

    @property
    def bambu_client(self):
        if self._bambu_client is None:
            raise ValueError("No connection to Bambulab was established")
        return self._bambu_client

    @property
    def is_running(self):
        return self._running

    @property
    def current_state(self):
        return self._current_state

    @property
    def current_print_job(self):
        if isinstance(self._current_state, PrintingState):
            return self._current_state.print_job
        return None

    def change_state(self, new_state: APrinterState):
        if self._current_state == new_state:
            return
        self._log.debug(
            f"Changing state from {self._current_state.__class__.__name__} to {new_state.__class__.__name__}"
        )

        self._current_state.finalize()
        self._current_state = new_state
        self._current_state.init()

    def new_update(self, event_type):
        if event_type == "event_hms_errors":
            self._update_hms_errors()
        elif event_type == "event_printer_data_update":
            self._update_printer_info()

    def _update_printer_info(self):
        device_data = self.bambu_client.get_device()
        ams = device_data.ams.__dict__
        print_job = device_data.print_job
        temperatures = device_data.temperature.__dict__
        lights = device_data.lights.__dict__
        fans = device_data.fans.__dict__
        speed = device_data.speed.__dict__

        self.lastTempAt = time.monotonic()
        self._telemetry.temp[0] = temperatures.get("nozzle_temp", 0.0)
        self._telemetry.targetTemp[0] = temperatures.get("target_nozzle_temp", 0.0)
        self.bedTemp = temperatures.get("bed_temp", 0.0)
        self.bedTargetTemp = temperatures.get("target_bed_temp", 0.0)
        self.chamberTemp = temperatures.get("chamber_temp", 0.0)

        if print_job.gcode_state == "RUNNING":
            self.change_state(self._state_printing)
        if print_job.gcode_state == "PAUSE":
            self.change_state(self._state_paused)
        if print_job.gcode_state == "FINISH" or print_job.gcode_state == "FAILED":
            self.change_state(self._state_finished)

    def _update_hms_errors(self):
        bambu_printer = self.bambu_client.get_device()
        if (
            bambu_printer.hms.errors != self._last_hms_errors
            and bambu_printer.hms.errors["Count"] > 0
        ):
            self._log.debug(f"HMS Error: {bambu_printer.hms.errors}")
            for n in range(1, bambu_printer.hms.errors["Count"] + 1):
                error = bambu_printer.hms.errors[f"{n}-Error"].strip()
                self.sendIO(f"// action:notification {error}")
            self._last_hms_errors = bambu_printer.hms.errors

    def on_disconnect(self, on_disconnect):
        self._log.debug(f"on disconnect called")
        return on_disconnect

    def on_connect(self, on_connect):
        self._log.debug(f"on connect called")
        return on_connect

    async def _create_connection_async(self):
        if (
            self._settings.get(["device_type"]) == ""
            or self._settings.get(["serial"]) == ""
            or self._settings.get(["username"]) == ""
            or self._settings.get(["access_code"]) == ""
        ):
            self._log.debug("invalid settings to start connection with Bambu Printer")
            return

        self._log.debug(
            f"connecting via local mqtt: {self._settings.get_boolean(['local_mqtt'])}"
        )
        self._bambu_client = BambuClient(
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
        self._bambu_client.on_disconnect = self.on_disconnect(
            self._bambu_client.on_disconnect
        )
        self._bambu_client.on_connect = self.on_connect(self._bambu_client.on_connect)
        self._bambu_client.connect(callback=self.new_update)
        self._log.info(f"bambu connection status: {self._bambu_client.connected}")
        self._serial_io.sendOk()

    def __str__(self):
        return "BAMBU(read_timeout={read_timeout},write_timeout={write_timeout},options={options})".format(
            read_timeout=self._read_timeout,
            write_timeout=self._write_timeout,
            options={
                "device_type": self._settings.get(["device_type"]),
                "host": self._settings.get(["host"]),
            },
        )

    def _reset(self):
        with self._serial_io.incoming_lock:
            self.lastN = 0
            self._running = False

            if self._sdstatus_reporter is not None:
                self._sdstatus_reporter.cancel()
                self._sdstatus_reporter = None

            if self._settings.get_boolean(["simulateReset"]):
                for item in self._settings.get(["resetLines"]):
                    self.sendIO(item + "\n")

            self._serial_io.reset()

    @property
    def timeout(self):
        return self._read_timeout

    @timeout.setter
    def timeout(self, value):
        self._log.debug(f"Setting read timeout to {value}s")
        self._read_timeout = value

    @property
    def write_timeout(self):
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value):
        self._log.debug(f"Setting write timeout to {value}s")
        self._write_timeout = value

    @property
    def port(self):
        return "BAMBU"

    @property
    def baudrate(self):
        return self._faked_baudrate

    def write(self, data: bytes) -> int:
        return self._serial_io.write(data)

    def readline(self) -> bytes:
        return self._serial_io.readline()

    def readlines(self) -> list[bytes]:
        result = []
        next_line = self._serial_io.readline()
        while next_line != b"":
            result.append(next_line)
            next_line = self._serial_io.readline()
        return result

    def sendIO(self, line: str):
        self._serial_io.send(line)

    def sendOk(self):
        self._serial_io.sendOk()

    def flush(self):
        self._serial_io.flush()

    ##~~ command implementations

    @gcode_executor.register("M23")
    def _select_sd_file(self, data: str) -> bool:
        filename = data.split(maxsplit=1)[1].strip()
        self.file_system.select_file(filename)
        return True

    @gcode_executor.register("M26")
    def _set_sd_position(self, data: str) -> bool:
        if data == "M26 S0":
            return self._cancel_print()
        else:
            self._log.debug("ignoring M26 command.")
            self.sendIO("M26 disabled for Bambu")
            return True

    @gcode_executor.register("M27")
    def _report_sd_print_status(self, data: str) -> bool:
        matchS = re.search(r"S([0-9]+)", data)
        if matchS:
            interval = int(matchS.group(1))
            if self._sdstatus_reporter is not None:
                self._sdstatus_reporter.cancel()

            if interval > 0:
                self._sdstatus_reporter = RepeatedTimer(
                    interval, self.report_print_job_status
                )
                self._sdstatus_reporter.start()
            else:
                self._sdstatus_reporter = None

        self.report_print_job_status()
        return True

    @gcode_executor.register("M30")
    def _delete_sd_file(self, data: str) -> bool:
        filename = data.split(None, 1)[1].strip()
        self.file_system.delete_file(filename)
        return True

    @gcode_executor.register("M105")
    def _report_temperatures(self, data: str) -> bool:
        return self._processTemperatureQuery()

    # noinspection PyUnusedLocal
    @gcode_executor.register("M115")
    def _report_firmware_info(self, data: str) -> bool:
        self.sendIO("Bambu Printer Integration")
        self.sendIO("Cap:EXTENDED_M20:1")
        self.sendIO("Cap:LFN_WRITE:1")
        self.sendIO("Cap:LFN_WRITE:1")
        return True

    @gcode_executor.register("M117")
    def _get_lcd_message(self, data: str) -> bool:
        # we'll just use this to echo a message, to allow playing around with pause triggers
        result = re.search(r"M117\s+(.*)", data).group(1)
        self.sendIO(f"echo:{result}")
        return True

    @gcode_executor.register("M118")
    def _serial_print(self, data: str) -> bool:
        match = re.search(r"M118 (?:(?P<parameter>A1|E1|Pn[012])\s)?(?P<text>.*)", data)
        if not match:
            self.sendIO("Unrecognized command parameters for M118")
        else:
            result = match.groupdict()
            text = result["text"]
            parameter = result["parameter"]

            if parameter == "A1":
                self.sendIO(f"//{text}")
            elif parameter == "E1":
                self.sendIO(f"echo:{text}")
            else:
                self.sendIO(text)
        return True

    # noinspection PyUnusedLocal
    @gcode_executor.register("M220")
    def _set_feedrate_percent(self, data: str) -> bool:
        if self.bambu_client.connected:
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
            if self.bambu_client.publish(gcode_command):
                self._log.info(f"{percent}% speed adjustment command sent successfully")
        return True

    def _process_gcode_serial_command(
        self, gcode_letter: str, gcode: str, full_command: str
    ):
        self._log.debug(
            f"processing gcode command letter = {gcode_letter} | gcode = {gcode} | full = {full_command}"
        )
        if gcode_letter in self.gcode_executor:
            handled = self.gcode_executor.execute(self, gcode_letter, full_command)
        else:
            handled = self.gcode_executor.execute(self, gcode, full_command)
        if handled:
            self._serial_io.sendOk()
            return

        # post gcode to printer otherwise
        if self.bambu_client.connected:
            GCODE_COMMAND = commands.SEND_GCODE_TEMPLATE
            GCODE_COMMAND["print"]["param"] = full_command + "\n"
            if self.bambu_client.publish(GCODE_COMMAND):
                self._log.info("command sent successfully")
                self._serial_io.sendOk()

    @gcode_executor.register_no_data("M112")
    def _shutdown(self):
        self._running = True
        if self.bambu_client.connected:
            self.bambu_client.disconnect()
        self.sendIO("echo:EMERGENCY SHUTDOWN DETECTED. KILLED.")
        self._serial_io.close()
        return True

    @gcode_executor.register_no_data("M20")
    def _list_sd(self):
        self.sendIO("Begin file list")
        for item in map(lambda f: f.get_log_info(), self.file_system.get_all_files()):
            self.sendIO(item)
        self.sendIO("End file list")
        return True

    @gcode_executor.register_no_data("M24")
    def _start_print(self):
        self._current_state.start_new_print()
        return True

    @gcode_executor.register_no_data("M25")
    def _pause_print(self):
        self._current_state.pause_print()
        return True

    @gcode_executor.register("M524")
    def _cancel_print(self):
        self._current_state.cancel_print()
        return True

    def report_print_job_status(self):
        print_job = self.current_print_job
        if print_job is not None:
            self.sendIO(
                f"SD printing byte {print_job.file_position}/{print_job.file_info.size}"
            )
        else:
            self.sendIO("Not SD printing")

    def _generateTemperatureOutput(self) -> str:
        template = "{heater}:{actual:.2f}/ {target:.2f}"
        temps = collections.OrderedDict()
        temps["T"] = (self._telemetry.temp[0], self._telemetry.targetTemp[0])
        temps["B"] = (self.bedTemp, self.bedTargetTemp)
        if self._telemetry.hasChamber:
            temps["C"] = (self.chamberTemp, self._telemetry.chamberTargetTemp)

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
        if self.bambu_client.connected:
            output = self._generateTemperatureOutput()
            self.sendIO(output)
            return True
        else:
            return False

    def _writeSdFile(self, filename: str) -> None:
        self.sendIO(f"Writing to file: {filename}")

    def _finishSdFile(self):
        # FIXME: maybe remove or move to remote SD card
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
        self.sendIO("Done saving file")

    def _setMainThreadBusy(self, reason="processing"):
        def loop():
            while self._busy_reason is not None:
                self.sendIO(f"echo:busy {self._busy_reason}")
                time.sleep(self._busy_interval)
            self._serial_io.sendOk()

        self._busy_reason = reason
        self._busy_loop = threading.Thread(target=loop)
        self._busy_loop.daemon = True
        self._busy_loop.start()

    def _setMainThreadIdle(self):
        self._busy_reason = None

    def close(self):
        if self.bambu_client.connected:
            self.bambu_client.disconnect()
        self._serial_io.close()

    def _showPrompt(self, text, choices):
        self._hidePrompt()
        self.sendIO(f"//action:prompt_begin {text}")
        for choice in choices:
            self.sendIO(f"//action:prompt_button {choice}")
        self.sendIO("//action:prompt_show")

    def _hidePrompt(self):
        self.sendIO("//action:prompt_end")
