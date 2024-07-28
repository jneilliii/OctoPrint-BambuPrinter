from __future__ import annotations
from pathlib import Path

from octoprint_bambu_printer.printer.file_system.file_info import FileInfo
from octoprint_bambu_printer.printer.print_job import PrintJob
from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState


class IdleState(APrinterState):

    def start_new_print(self):
        selected_file = self._printer.selected_file
        if selected_file is None:
            self._log.warn("Cannot start print job if file was not selected")
            return

        print_command = self._get_print_command_for_file(selected_file)
        self._log.debug(f"Sending print command: {print_command}")
        if self._printer.bambu_client.publish(print_command):
            self._log.info(f"Started print for {selected_file.file_name}")
        else:
            self._log.warn(f"Failed to start print for {selected_file.file_name}")

    def _get_print_command_for_file(self, selected_file: FileInfo):

        # URL to print. Root path, protocol can vary. E.g., if sd card, "ftp:///myfile.3mf", "ftp:///cache/myotherfile.3mf"
        filesystem_root = (
            "file:///mnt/sdcard/"
            if self._printer._settings.get_boolean(["device_type"]) in ["X1", "X1C"]
            else "file:///"
        )

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
                "subtask_name": selected_file.file_name,
                "url": f"{filesystem_root}{selected_file.path.as_posix()}",
                "bed_type": "auto",
                "timelapse": self._printer._settings.get_boolean(["timelapse"]),
                "bed_leveling": self._printer._settings.get_boolean(["bed_leveling"]),
                "flow_cali": self._printer._settings.get_boolean(["flow_cali"]),
                "vibration_cali": self._printer._settings.get_boolean(
                    ["vibration_cali"]
                ),
                "layer_inspect": self._printer._settings.get_boolean(["layer_inspect"]),
                "use_ams": self._printer._settings.get_boolean(["use_ams"]),
                "ams_mapping": "",
            }
        }

        return print_command
