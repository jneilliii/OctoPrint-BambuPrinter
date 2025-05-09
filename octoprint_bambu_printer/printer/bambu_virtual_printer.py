from __future__ import annotations

import collections
from dataclasses import dataclass, field, asdict
import math
from pathlib import Path
import queue
import re
import threading
import time
from octoprint_bambu_printer.printer.file_system.cached_file_view import CachedFileView
from octoprint_bambu_printer.printer.file_system.file_info import FileInfo
from octoprint_bambu_printer.printer.print_job import PrintJob
from octoprint_bambu_printer.printer.pybambu import BambuClient, commands
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
    ams_current_tray: int = 255


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
        self._settings = settings
        self._printer_profile_manager = printer_profile_manager
        self._faked_baudrate = faked_baudrate
        self._data_folder = data_folder
        self._last_hms_errors = None
        self._log = logging.getLogger("octoprint.plugins.bambu_printer.BambuPrinter")
        self.ams_data = self._settings.get(["ams_data"])

        self._state_idle = IdleState(self)
        self._state_printing = PrintingState(self)
        self._state_paused = PausedState(self)
        self._current_state = self._state_idle

        self._running = True
        self._print_status_reporter = None
        self._print_temp_reporter = None
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
        self._selected_project_file: FileInfo | None = None
        self._project_files_view = (
            CachedFileView(self.file_system, on_update=self._list_cached_project_files)
            .with_filter("", ".3mf")
            .with_filter("cache/", ".3mf")
        )

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

    @property
    def selected_file(self):
        return self._selected_project_file

    @property
    def has_selected_file(self):
        return self._selected_project_file is not None

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

    @property
    def project_files(self):
        return self._project_files_view

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
        # strip out extra data to avoid unneeded settings updates
        ams_data = [{"tray": asdict(x).pop("tray", None)} for x in device_data.ams.data if x is not None]

        if self.ams_data != ams_data:
            self._log.debug(f"Recieveid AMS Update: {ams_data}")
            self.ams_data = ams_data
            self._settings.set(["ams_data"], ams_data)
            self._settings.save(trigger_event=True)

        self.lastTempAt = time.monotonic()
        self._telemetry.temp[0] = temperatures.nozzle_temp
        self._telemetry.targetTemp[0] = temperatures.target_nozzle_temp
        self._telemetry.bedTemp = temperatures.bed_temp
        self._telemetry.bedTargetTemp = temperatures.target_bed_temp
        self._telemetry.chamberTemp = temperatures.chamber_temp
        if device_data.push_all_data and "ams" in device_data.push_all_data:
            self._telemetry.ams_current_tray = device_data.push_all_data["ams"]["tray_now"] or 255

        if self._telemetry.ams_current_tray != self._settings.get_int(["ams_current_tray"]):
            self._settings.set_int(["ams_current_tray"], self._telemetry.ams_current_tray)
            self._settings.save(trigger_event=True)

        self._log.debug(f"Received printer state update: {print_job_state}")
        if (
            print_job_state == "IDLE"
            or print_job_state == "FINISH"
            or print_job_state == "FAILED"
        ):
            self.change_state(self._state_idle)
        elif print_job_state == "RUNNING" or print_job_state == "PREPARE":
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
        self.stop_continuous_status_report()
        self.stop_continuous_temp_report()
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
            {"device_type": self._settings.get(["device_type"]),
            "serial": self._settings.get(["serial"]),
            "host": self._settings.get(["host"]),
            "username": (
                "bblp"
                if self._settings.get_boolean(["local_mqtt"])
                else self._settings.get(["username"])
            ),
            "access_code": self._settings.get(["access_code"]),
            "local_mqtt": self._settings.get_boolean(["local_mqtt"]),
            "region": self._settings.get(["region"]),
            "email": self._settings.get(["email"]),
            "auth_token": self._settings.get(["auth_token"]) if self._settings.get_boolean(["local_mqtt"]) is False else "",
             }
        )
        bambu_client.on_disconnect = self.on_disconnect(bambu_client.on_disconnect)
        bambu_client.on_connect = self.on_connect(bambu_client.on_connect)
        bambu_client.connect(callback=self.new_update)
        self._log.debug(f"bambu connection status: {bambu_client.connected}")
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

            if self._print_status_reporter is not None:
                self._print_status_reporter.cancel()
                self._print_status_reporter = None

            if self._settings.get_boolean(["simulateReset"]):
                for item in self._settings.get(["resetLines"]):
                    self.sendIO(item + "\n")

            self._serial_io.reset()

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

    ##~~ project file functions

    def remove_project_selection(self):
        self._log.debug("Removing project selection.")
        self._selected_project_file = None
        # ** Add call to send message after deselection **
        # This will make _send_file_selected_message send the "deselected" message
        self._send_file_selected_message()
        # Ensure _serial_io.reset() is *not* called here based on previous issues (and it's not in your current script)



    def select_project_file(self, file_path: str) -> bool:
        file_info = self._project_files_view.get_file_by_name(file_path)
        if (
            self._selected_project_file is not None
            and file_info is not None
            and self._selected_project_file.path == file_info.path
        ):
            self._log.debug(f"File already selected: {file_path}")
            return True

        if file_info is None:
            self._log.error(f"Cannot select non-existent file: {file_path}")
            return False

        self._log.debug(f"Select project file: {file_path}")

        self._selected_project_file = file_info
        self._send_file_selected_message()
        return True


    @gcode_executor.register_no_data("M21")
    def _sd_status(self) -> bool:
        self.sendIO("SD card ok")
        return True

    @gcode_executor.register("M23")
    def _select_sd_file(self, data: str) -> bool:
        self._log.debug("M23 command received.")
        filename = data.split(maxsplit=1)[1].strip()

        # ** Step 1: Perform the deselection logic **
        self._log.debug("Calling remove_project_selection as part of M23 handling.")
        # Call the remove_project_selection method to clear any previous selection state
        self.remove_project_selection()
        # remove_project_selection will call _send_file_selected_message
        # and send the explicit deselection messages over serial.

        # Add a small delay here to allow OctoPrint's UI to potentially process
        # the deselection messages before the selection messages arrive.
        # This might help synchronize the UI state.
        time.sleep(1) # Small delay (50 milliseconds)

        # ** Step 2: Proceed with the original M23 selection logic **
        self._log.debug(f"Proceeding with selection for filename: {filename}")
        # Call the select_project_file method to set the new selection
        # select_project_file will call _send_file_selected_message
        # and send the selection messages for the new file.
        success = self.select_project_file(filename)

        # The "ok N+1" response for the M23 command is handled automatically
        # by _process_gcode_serial_command after this method returns.

        return success # Return whether the selection was successful


    def _send_file_selected_message(self):
        if self.selected_file is None:
            return

        self.sendIO(f"File opened: {self.selected_file.dosname} Size: {self.selected_file.size}")
        self.sendIO("File selected")



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
            if interval > 0:
                self.start_continuous_status_report(interval)
                return False
            else:
                self.stop_continuous_status_report()
                return False

        self.report_print_job_status()
        return True

    def start_continuous_status_report(self, interval: int):
        if self._print_status_reporter is not None:
            self._print_status_reporter.cancel()

        self._print_status_reporter = RepeatedTimer(
            interval, self.report_print_job_status
        )
        self._print_status_reporter.start()

    def stop_continuous_status_report(self):
        if self._print_status_reporter is not None:
            self._print_status_reporter.cancel()
            self._print_status_reporter = None

    @gcode_executor.register("M30")
    def _delete_project_file(self, data: str) -> bool:
        file_path = data.split(maxsplit=1)[1].strip()
        file_info = self.project_files.get_file_data(file_path)
        if file_info is not None:
            self.file_system.delete_file(file_info.path)
            self._update_project_file_list()
        else:
            self._log.error(f"File not found to delete {file_path}")
        return True

    @gcode_executor.register("M105")
    def _report_temperatures(self, data: str) -> bool:
        self._processTemperatureQuery()
        return True

    @gcode_executor.register("M155")
    def _auto_report_temperatures(self, data: str) -> bool:
        matchS = re.search(r"S([0-9]+)", data)
        if matchS:
            interval = int(matchS.group(1))
            if interval > 0:
                self.start_continuous_temp_report(interval)
            else:
                self.stop_continuous_temp_report()

        self.report_print_job_status()
        return True

    def start_continuous_temp_report(self, interval: int):
        if self._print_temp_reporter is not None:
            self._print_temp_reporter.cancel()

        self._print_temp_reporter = RepeatedTimer(
            interval, self._processTemperatureQuery
        )
        self._print_temp_reporter.start()

    def stop_continuous_temp_report(self):
        if self._print_temp_reporter is not None:
            self._print_temp_reporter.cancel()
            self._print_temp_reporter = None



    # noinspection PyUnusedLocal
    @gcode_executor.register_no_data("M115")
    def _report_firmware_info(self) -> bool:
        # wait for connection to be established before sending back firmware info
        while self.bambu_client.connected is False:
            time.sleep(1)
        self.sendIO("Bambu Printer Integration")
        self.sendIO("Cap:AUTOREPORT_SD_STATUS:1")
        self.sendIO("Cap:AUTOREPORT_TEMP:1")
        self.sendIO("Cap:EXTENDED_M20:1")
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
            percent = int(data.replace("M220 S", ""))

            def speed_fraction(speed_percent):
                return math.floor(10000 / speed_percent) / 100

            def acceleration_magnitude(speed_percent):
                return math.exp((speed_fraction(speed_percent) - 1.0191) / -0.8139)

            def feed_rate(speed_percent):
                return 6.426e-5 * speed_percent ** 2 - 2.484e-3 * speed_percent + 0.654

            def linear_interpolate(x, x_points, y_points):
                if x <= x_points[0]: return y_points[0]
                if x >= x_points[-1]: return y_points[-1]
                for i in range(len(x_points) - 1):
                    if x_points[i] <= x < x_points[i + 1]:
                        t = (x - x_points[i]) / (x_points[i + 1] - x_points[i])
                        return y_points[i] * (1 - t) + y_points[i + 1] * t

            def scale_to_data_points(func, data_points):
                data_points.sort(key=lambda x: x[0])
                speeds, values = zip(*data_points)
                scaling_factors = [v / func(s) for s, v in zip(speeds, values)]
                return lambda x: func(x) * linear_interpolate(x, speeds, scaling_factors)

            def speed_adjust(speed_percentage):
                if not 30 <= speed_percentage <= 180:
                    speed_percentage = 100

                bambu_params = {
                    "speed": [50, 100, 124, 166],
                    "acceleration": [0.3, 1.0, 1.4, 1.6],
                    "feed_rate": [0.7, 1.0, 1.4, 2.0]
                }

                acc_mag_scaled = scale_to_data_points(acceleration_magnitude,
                                                      list(zip(bambu_params["speed"], bambu_params["acceleration"])))
                feed_rate_scaled = scale_to_data_points(feed_rate,
                                                        list(zip(bambu_params["speed"], bambu_params["feed_rate"])))

                speed_frac = speed_fraction(speed_percentage)
                acc_mag = acc_mag_scaled(speed_percentage)
                feed = feed_rate_scaled(speed_percentage)
                # speed_level = 1.539 * (acc_mag**2) - 0.7032 * acc_mag + 4.0834
                return f"M204.2 K{acc_mag:.2f}\nM220 K{feed:.2f}\nM73.2 R{speed_frac:.2f}\n" # M1002 set_gcode_claim_speed_level ${speed_level:.0f}\n

            speed_command = speed_adjust(percent)

            gcode_command["print"]["param"] = speed_command
            if self.bambu_client.publish(gcode_command):
                self._log.debug(f"{percent}% speed adjustment command sent successfully")
        return True

    def _process_gcode_serial_command(self, gcode: str, full_command: str):
        self._log.debug(f"processing gcode {gcode} command = {full_command}")

        # Execute the command handler
        handled = self.gcode_executor.execute(self, gcode, full_command)

        # ** Modify the response sending logic **
        # Regardless of whether it was handled by a local executor or sent via MQTT,
        # we need to send an "ok" response back to OctoPrint via the simulated serial.
        # This response should include the next expected line number.

        # Get the next expected line number from PrinterSerialIO's state
        # Make sure to access lastN via self._serial_io
        next_expected_line = self._serial_io.lastN + 1

        if handled:
            self._log.debug(f"G-code command {gcode} handled internally. Sending ok {next_expected_line}")
            # Send "ok N+1" back to OctoPrint via PrinterSerialIO
            self._serial_io.send(f"ok {next_expected_line}\n")
            return

        # If not handled by a local executor, post gcode to printer otherwise
        if self.bambu_client.connected:
            GCODE_COMMAND = commands.SEND_GCODE_TEMPLATE
            GCODE_COMMAND["print"]["param"] = full_command + "\n"
            if self.bambu_client.publish(GCODE_COMMAND):
                self._log.debug(f"command {gcode} sent successfully via MQTT. Sending ok {next_expected_line}")
                # Send "ok N+1" back to OctoPrint via PrinterSerialIO
                self._serial_io.send(f"ok {next_expected_line}\n")
            else:
                self._log.warning(f"Failed to send command {gcode} via MQTT.")
                # Optionally send an error response back to OctoPrint
                # self._serial_io.send(f"Error: MQTT send failed for {gcode}\n")
        else:
             self._log.warning(f"Printer not connected, cannot send command {gcode} via MQTT.")
             # Optionally send an error response back to OctoPrint
             # self._serial_io.send(f"Error: Printer not connected, cannot execute {gcode}\n")

    @gcode_executor.register_no_data("M112")
    def _shutdown(self):
        self._running = True
        if self.bambu_client.connected:
            self.bambu_client.disconnect()
        self.sendIO("echo:EMERGENCY SHUTDOWN DETECTED. KILLED.")
        self._serial_io.close()
        return True

    @gcode_executor.register("M20")
    def _update_project_file_list(self, data: str = ""):
        self._project_files_view.update()  # internally sends list to serial io
        return True

    def _list_cached_project_files(self):
        self.sendIO("Begin file list")
        for item in map(
            FileInfo.get_gcode_info, self._project_files_view.get_all_cached_info()
        ):
            self.sendIO(item)
        self.sendIO("End file list")
        self.sendOk()

    @gcode_executor.register_no_data("M24")
    def _start_resume_sd_print(self):
        self._current_state.start_new_print()
        return True

    @gcode_executor.register_no_data("M25")
    def _pause_print(self):
        self._current_state.pause_print()
        return True

    @gcode_executor.register("M355")
    def _case_lights(self, data: str) -> bool:
        if data == "M355 S1":
            light_command = commands.CHAMBER_LIGHT_ON
        elif data == "M355 S0":
            light_command = commands.CHAMBER_LIGHT_OFF
        else:
            return False

        return self.bambu_client.publish(light_command)

    @gcode_executor.register("M524")
    def _cancel_print(self):
        self._current_state.cancel_print()
        time.sleep(5)
        self.remove_project_selection()
        return True

    def report_print_job_status(self):
        if self.current_print_job is not None:
            file_position = 1 if self.current_print_job.file_position == 0 else self.current_print_job.file_position
            self.sendIO(
                f"SD printing byte {file_position}"
                f"/{self.current_print_job.file_info.size}"
            )
        else:
            self.sendIO("Not SD printing")

    def report_print_finished(self):
        if self.current_print_job is None:
            return
        self._log.debug(
            f"SD File Print finishing: {self.current_print_job.file_info.file_name}"
        )
        self.sendIO("Done printing file")

    def finalize_print_job(self):
        if self.current_print_job is not None:
            self.report_print_job_status()
            self.report_print_finished()
            self.current_print_job = None
            self.remove_project_selection()
            self.report_print_job_status()
        self.change_state(self._state_idle)
        time.sleep(5)
        self.remove_project_selection()

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
        # self._create_client_connection_async()
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

        # Check if the new state is the IdleState (self._state_idle is the instance of IdleState)
        if new_state == self._state_idle:
            self._log.debug("Transitioned to Idle state. Applying cleanup delay and removing selection.")


        self._current_state.init()

    def _showPrompt(self, text, choices):
        self._hidePrompt()
        self.sendIO(f"//action:prompt_begin {text}")
        for choice in choices:
            self.sendIO(f"//action:prompt_button {choice}")
        self.sendIO("//action:prompt_show")

    def _hidePrompt(self):
        self.sendIO("//action:prompt_end")
