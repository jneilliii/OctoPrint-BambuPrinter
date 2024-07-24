from __future__ import annotations

from octoprint_bambu_printer.printer.print_job import PrintJob
from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState


class IdleState(APrinterState):

    def init(self):
        if self._printer.file_system.has_selected_file:
            self.update_print_job_info()

    def start_new_print(self):
        selected_file = self._printer.file_system.selected_file
        if selected_file is None:
            self._log.warn("Cannot start print job if file was not selected")
            return

        print_command = self._get_print_command_for_file(selected_file)
        if self._printer.bambu_client.publish(print_command):
            self._log.info(f"Started print for {selected_file.file_name}")
            self._printer.change_state(self._printer._state_printing)
        else:
            self._log.warn(f"Failed to start print for {selected_file.file_name}")
            self._printer.change_state(self._printer._state_idle)

    def _get_print_command_for_file(self, selected_file):
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
                "subtask_name": f"{selected_file}",
                "file": f"{selected_file}",
                "url": (
                    f"file:///mnt/sdcard/{selected_file}"
                    if self._printer._settings.get_boolean(["device_type"])
                    in ["X1", "X1C"]
                    else f"file:///sdcard/{selected_file}"
                ),
                "timelapse": self._printer._settings.get_boolean(["timelapse"]),
                "bed_leveling": self._printer._settings.get_boolean(["bed_leveling"]),
                "flow_cali": self._printer._settings.get_boolean(["flow_cali"]),
                "vibration_cali": self._printer._settings.get_boolean(
                    ["vibration_cali"]
                ),
                "layer_inspect": self._printer._settings.get_boolean(["layer_inspect"]),
                "use_ams": self._printer._settings.get_boolean(["use_ams"]),
            }
        }

        return print_command

    def update_print_job_info(self):
        if self._printer.file_system.selected_file is not None:
            self._printer.current_print_job = PrintJob(
                self._printer.file_system.selected_file, 0
            )
