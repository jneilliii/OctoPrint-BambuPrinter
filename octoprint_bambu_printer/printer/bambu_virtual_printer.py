from __future__ import annotations

import collections
from dataclasses import dataclass, field
import math
import queue
import re
import threading
import time
from octoprint_bambu_printer.printer.print_job import PrintJob
from pybambu import BambuClient, commands
import logging
import logging.handlers

from octoprint.util import RepeatedTimer

from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState
from octoprint_bambu_printer.printer.states.idle_state import IdleState

from .printer_serial_io import PrinterSerialIO
from .states.paused_state import PausedState
from .states.printing_state import PrintingState

from .gcode_executor import GCodeExecutor
from .file_system.remote_sd_card_file_list import RemoteSDCardFileList


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
        self._current_state = self._state_idle

        self._running = True
        self._printer_thread = threading.Thread(
            target=self._printer_worker,
            name="octoprint.plugins.bambu_printer.printer_state",
        )
        self._state_change_queue = queue.Queue()

        self._current_print_job: PrintJob | None = None

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

        self.file_system = RemoteSDCardFileList(settings)

        self._settings = settings
        self._printer_profile_manager = printer_profile_manager
        self._faked_baudrate = faked_baudrate

        self._last_hms_errors = None

        self._serial_io.start()
        self._printer_thread.start()

        self._bambu_client: BambuClient = self._create_client_connection_async()

    @property
    def bambu_client(self):
        return self._bambu_client

    @property
    def is_running(self):
        return self._running

    @property
    def current_state(self):
        return self._current_state

    @property
    def current_print_job(self):
        return self._current_print_job

    @current_print_job.setter
    def current_print_job(self, value):
        self._current_print_job = value

    def change_state(self, new_state: APrinterState):
        self._state_change_queue.put(new_state)

    def new_update(self, event_type):
        if event_type == "event_hms_errors":
            self._update_hms_errors()
        elif event_type == "event_printer_data_update":
            self._update_printer_info()

    def _update_printer_info(self):
        device_data = self.bambu_client.get_device()
        print_job_state = device_data.print_job.gcode_state
        temperatures = device_data.temperature

        self.lastTempAt = time.monotonic()
        self._telemetry.temp[0] = temperatures.nozzle_temp
        self._telemetry.targetTemp[0] = temperatures.target_nozzle_temp
        self._telemetry.bedTemp = temperatures.bed_temp
        self._telemetry.bedTargetTemp = temperatures.target_bed_temp
        self._telemetry.chamberTemp = temperatures.chamber_temp

        if (
            print_job_state == "IDLE"
            or print_job_state == "FINISH"
            or print_job_state == "FAILED"
        ):
            self.change_state(self._state_idle)
        elif print_job_state == "RUNNING":
            self.change_state(self._state_printing)
        elif print_job_state == "PAUSE":
            self.change_state(self._state_paused)
        else:
            self._log.warn(f"Unknown print job state: {print_job_state}")

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

    def _create_client_connection_async(self):
        self._create_client_connection()
        if self._bambu_client is None:
            raise RuntimeError("Connection with Bambu Client not established")
        return self._bambu_client

    def _create_client_connection(self):
        if (
            self._settings.get(["device_type"]) == ""
            or self._settings.get(["serial"]) == ""
            or self._settings.get(["username"]) == ""
            or self._settings.get(["access_code"]) == ""
        ):
            msg = "invalid settings to start connection with Bambu Printer"
            self._log.debug(msg)
            raise ValueError(msg)

        self._log.debug(
            f"connecting via local mqtt: {self._settings.get_boolean(['local_mqtt'])}"
        )
        bambu_client = BambuClient(
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
        bambu_client.on_disconnect = self.on_disconnect(bambu_client.on_disconnect)
        bambu_client.on_connect = self.on_connect(bambu_client.on_connect)
        bambu_client.connect(callback=self.new_update)
        self._log.info(f"bambu connection status: {bambu_client.connected}")
        self.sendOk()
        self._bambu_client = bambu_client

    def __str__(self):
        return "BAMBU(read_timeout={read_timeout},write_timeout={write_timeout},options={options})".format(
            read_timeout=self.timeout,
            write_timeout=self.write_timeout,
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
        return self._serial_io._read_timeout

    @timeout.setter
    def timeout(self, value):
        self._log.debug(f"Setting read timeout to {value}s")
        self._serial_io._read_timeout = value

    @property
    def write_timeout(self):
        return self._serial_io._write_timeout

    @write_timeout.setter
    def write_timeout(self, value):
        self._log.debug(f"Setting write timeout to {value}s")
        self._serial_io._write_timeout = value

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
        return self._serial_io.readlines()

    def sendIO(self, line: str):
        self._serial_io.send(line)

    def sendOk(self):
        self._serial_io.sendOk()

    def flush(self):
        self._serial_io.flush()
        self._wait_for_state_change()

    ##~~ command implementations

    @gcode_executor.register_no_data("M21")
    def _sd_status(self) -> None:
        self.sendIO("SD card ok")

    @gcode_executor.register("M23")
    def _select_sd_file(self, data: str) -> bool:
        filename = data.split(maxsplit=1)[1].strip()
        self._list_sd()
        if not self.file_system.select_project_file(filename):
            return False

        assert self.file_system.selected_file is not None
        self._current_state.update_print_job_info()

        self.sendIO(
            f"File opened: {self.file_system.selected_file.file_name}  "
            f"Size: {self.file_system.selected_file.size}"
        )
        self.sendIO("File selected")
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
        self._list_sd()
        self.file_system.delete_file(filename)
        return True

    @gcode_executor.register("M105")
    def _report_temperatures(self, data: str) -> bool:
        return self._processTemperatureQuery()

    # noinspection PyUnusedLocal
    @gcode_executor.register_no_data("M115")
    def _report_firmware_info(self) -> bool:
        self.sendIO("Bambu Printer Integration")
        self.sendIO("Cap:EXTENDED_M20:1")
        self.sendIO("Cap:LFN_WRITE:1")
        self.sendIO("Cap:LFN_WRITE:1")
        return True

    @gcode_executor.register("M117")
    def _get_lcd_message(self, data: str) -> bool:
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

    def _process_gcode_serial_command(self, gcode: str, full_command: str):
        self._log.debug(f"processing gcode {gcode} command = {full_command}")
        handled = self.gcode_executor.execute(self, gcode, full_command)
        if handled:
            self.sendOk()
            return

        # post gcode to printer otherwise
        if self.bambu_client.connected:
            GCODE_COMMAND = commands.SEND_GCODE_TEMPLATE
            GCODE_COMMAND["print"]["param"] = full_command + "\n"
            if self.bambu_client.publish(GCODE_COMMAND):
                self._log.info("command sent successfully")
                self.sendOk()

    @gcode_executor.register_no_data("M112")
    def _shutdown(self):
        self._running = True
        if self.bambu_client.connected:
            self.bambu_client.disconnect()
        self.sendIO("echo:EMERGENCY SHUTDOWN DETECTED. KILLED.")
        self._serial_io.close()
        return True

    @gcode_executor.register("M20")
    def _list_sd(self, data: str = ""):
        self.sendIO("Begin file list")
        for item in map(
            lambda f: f.get_log_info(), self.file_system.get_all_project_files()
        ):
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

    def _create_temperature_message(self) -> str:
        template = "{heater}:{actual:.2f}/ {target:.2f}"
        temps = collections.OrderedDict()
        temps["T"] = (self._telemetry.temp[0], self._telemetry.targetTemp[0])
        temps["B"] = (self._telemetry.bedTemp, self._telemetry.bedTargetTemp)
        if self._telemetry.hasChamber:
            temps["C"] = (
                self._telemetry.chamberTemp,
                self._telemetry.chamberTargetTemp,
            )

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
            output = self._create_temperature_message()
            self.sendIO(output)
            return True
        else:
            return False

    def close(self):
        if self.bambu_client.connected:
            self.bambu_client.disconnect()
        self.change_state(self._state_idle)
        self._serial_io.close()
        self.stop()

    def stop(self):
        self._running = False
        self._printer_thread.join()

    def _wait_for_state_change(self):
        self._state_change_queue.join()

    def _printer_worker(self):
        self._create_client_connection_async()
        self.sendIO("Printer connection complete")
        while self._running:
            try:
                next_state = self._state_change_queue.get(timeout=0.01)
                self._trigger_change_state(next_state)
                self._state_change_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                self._state_change_queue.task_done()
                raise e
        self._current_state.finalize()

    def _trigger_change_state(self, new_state: APrinterState):
        if self._current_state == new_state:
            return
        self._log.debug(
            f"Changing state from {self._current_state.__class__.__name__} to {new_state.__class__.__name__}"
        )

        self._current_state.finalize()
        self._current_state = new_state
        self._current_state.init()

    def _showPrompt(self, text, choices):
        self._hidePrompt()
        self.sendIO(f"//action:prompt_begin {text}")
        for choice in choices:
            self.sendIO(f"//action:prompt_button {choice}")
        self.sendIO("//action:prompt_show")

    def _hidePrompt(self):
        self.sendIO("//action:prompt_end")
