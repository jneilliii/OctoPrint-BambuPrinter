# coding=utf-8

#    Octoprint plugin for retrieving the estimated time of printing using the Prusa Mini.
#    Copyright (C) 2020 Michal Duda - github@vookimedlo.cz
#    https://github.com/vookimedlo/OctoPrint-Prusa-Mini-ETA
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

# pylint: disable=line-too-long

from __future__ import absolute_import

import os.path
import time
import json

import octoprint.filemanager.analysis
from octoprint.filemanager.analysis import AnalysisAborted
from octoprint.filemanager.analysis import QueueEntry
from octoprint.filemanager.util import DiskFileWrapper

try:
    from octoprint_PrintTimeGenius import GeniusAnalysisQueue
    BaseAnalysisQueue = GeniusAnalysisQueue
    requires_plugin_arg = True
except ImportError:
    from octoprint.filemanager.analysis import GcodeAnalysisQueue
    BaseAnalysisQueue = GcodeAnalysisQueue
    requires_plugin_arg = False


class BambuGcodeAnalysisQueue(BaseAnalysisQueue):
    """Initial estimation."""

    def __init__(self, finished_callback, plugin):
        self._plugin = plugin
        if requires_plugin_arg:
            super(BambuGcodeAnalysisQueue, self).__init__(finished_callback, plugin)
        else:
            super(BambuGcodeAnalysisQueue, self).__init__(finished_callback)

    def _do_analysis(self, high_priority=True):
        try:
            def throttle():
                time.sleep(0.01)  # high_priority == False

            found_eta = False

            result = super(BambuGcodeAnalysisQueue, self)._do_analysis(high_priority)

            if self._current.type != "3mf":
                return result

            path = self._current.path
            metadata_path = str(os.path.join(self._plugin.get_plugin_data_folder(), "gcode", path, "Metadata"))
            gcode_file_path = os.path.join(metadata_path, "plate_1.gcode")
            thumbnail_file_path = os.path.join(metadata_path, "plate_1.png")
            json_file_path = os.path.join(metadata_path, "plate_1.json")

            if not os.path.exists(metadata_path):
                file_object = DiskFileWrapper(path, self._current.absolute_path, move=False)
                self._plugin.process_3mf_upload(path, file_object)

            if os.path.exists(gcode_file_path):
                gcode_queue_entry = QueueEntry(self._current.name, path, "gcode", self._current.location, gcode_file_path, self._current.printer_profile, self._current.analysis)
                super(BambuGcodeAnalysisQueue, self).enqueue(gcode_queue_entry)

                with open(gcode_file_path, "r") as gcode_file:
                    for line in gcode_file:
                        if found_eta:
                            # Don't continue to parse the file
                            break

                        line = line.strip()  # Prevent surprises
                        if line.startswith("M73"):
                            self._plugin._logger.info("Found M73: %s", line)
                            command_parts = line.split()

                            for parameter in command_parts[1:]:  # Ignore already found M73
                                if parameter[0] == "R":
                                    found_eta = True
                                    result["estimatedPrintTime"] = int(parameter[1:]) * 60
                                    self._plugin._logger.info("New ETA from the upload: %s seconds", result["estimatedPrintTime"])
                                    break
                            continue

                        if not high_priority:
                            throttle()
                        if self._aborted:
                            # If abortion is requested do not raise AnalysisAborted, but return already
                            # estimatedPrintTime from the base class.
                            return result

            # set thumbnail attribute
            if os.path.exists(thumbnail_file_path):
                thumb_url = f"/plugin/bambu_printer/download/thumbs/{path}/Metadata/plate_1.png"
                self._plugin._file_manager.set_additional_metadata("local", path, "thumbnail_src", self._plugin._identifier, overwrite=True)
                self._plugin._file_manager.set_additional_metadata("local", path, "thumbnail", thumb_url, overwrite=True)

            # add plate data
            if os.path.exists(json_file_path):
                with open(json_file_path, "r") as json_file:
                    plate_data = json.load(json_file)

                if plate_data is not None:
                    self._plugin._file_manager.set_additional_metadata("local", path, "plate_data", plate_data, overwrite=True)

                # add metadata if SlicerEstimator installed.
                if self._plugin.se_update_metadata_in_file is not None:
                    self._plugin.se_update_metadata_in_file(path, gcode_file_path)

            return result
        except AnalysisAborted as _:
            raise

    def _do_abort(self, reenqueue=True):
        super(BambuGcodeAnalysisQueue, self)._do_abort(reenqueue)
