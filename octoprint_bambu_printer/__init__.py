# coding=utf-8
from __future__ import absolute_import

import threading
import time

import octoprint.plugin

from .ftpsclient import IoTFTPSClient


class BambuPrintPlugin(
    octoprint.plugin.SettingsPlugin, octoprint.plugin.TemplatePlugin
):

    def get_template_configs(self):
        return [{"type": "settings", "custom_bindings": False}]

    def get_settings_defaults(self):
        return {"device_type": "X1C",
                "serial": "",
                "host": "",
                "access_code": "",
                "username": "bblp",
                "timelapse": False,
                "bed_leveling": True,
                "flow_cali": False,
                "vibration_cali": True,
                "layer_inspect": True,
                "use_ams": False,
                "local_mqtt": True,
                "region": "",
                "email": "",
                "auth_token": ""}

    def support_3mf_files(self):
        return {'machinecode': {'3mf': ["3mf"]}}

    def upload_to_sd(self, printer, filename, path, sd_upload_started, sd_upload_succeeded, sd_upload_failed, *args, **kwargs):
        self._logger.debug(f"Starting upload from {filename} to {filename}")
        sd_upload_started(filename, filename)
        def process():
            host = self._settings.get(["host"])
            access_code = self._settings.get(["access_code"])
            elapsed = time.monotonic()

            try:
                ftp = IoTFTPSClient(f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True)
                if ftp.upload_file(path, f"{filename}"):
                    elapsed = time.monotonic() - elapsed
                    sd_upload_succeeded(filename, filename, elapsed)
                    # remove local file after successful upload to Bambu
                    self._file_manager.remove_file("local", filename)
                else:
                    raise Exception("upload failed")
            except Exception as e:
                elapsed = time.monotonic() - elapsed
                sd_upload_failed(filename, filename, elapsed)
                self._logger.debug(f"Error uploading file {filename}")

        thread = threading.Thread(target=process)
        thread.daemon = True
        thread.start()

        return filename

    def virtual_printer_factory(self, comm_instance, port, baudrate, read_timeout):
        if not port == "BAMBU":
            return None

        if self._settings.get(["serial"]) == "" or self._settings.get(["host"]) == "" or self._settings.get(["access_code"]) == "":
            return None

        import logging.handlers

        from octoprint.logging.handlers import CleaningTimedRotatingFileHandler

        seriallog_handler = CleaningTimedRotatingFileHandler(
            self._settings.get_plugin_logfile_path(postfix="serial"),
            when="D",
            backupCount=3,
        )
        seriallog_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        seriallog_handler.setLevel(logging.DEBUG)

        from . import virtual

        serial_obj = virtual.BambuPrinter(
            self._settings,
            self._printer_profile_manager,
            data_folder=self.get_plugin_data_folder(),
            seriallog_handler=seriallog_handler,
            read_timeout=float(read_timeout),
            faked_baudrate=baudrate,
        )
        return serial_obj

    def get_additional_port_names(self, *args, **kwargs):
        if self._settings.get(["serial"]) != "" and self._settings.get(["host"]) != "" and self._settings.get(["access_code"]) != "":
            return ["BAMBU"]
        else:
            return []

    def get_update_information(self):
        return {'bambu_printer': {'displayName': "Bambu Printer",
                                  'displayVersion': self._plugin_version,
                                  'type': "github_release",
                                  'user': "jneilliii",
                                  'repo': "OctoPrint-BambuPrinter",
                                  'current': self._plugin_version,
                                  'stable_branch': {'name': "Stable",
                                                    'branch': "master",
                                                    'comittish': ["master"]},
                                  'prerelease_branches': [
                                      {'name': "Release Candidate",
                                       'branch': "rc",
                                       'comittish': ["rc", "master"]}
                                  ],
                                  'pip': "https://github.com/jneilliii/OctoPrint-BambuPrinter/archive/{target_version}.zip"}}


__plugin_name__ = "Bambu Printer"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    plugin = BambuPrintPlugin()

    global __plugin_implementation__
    __plugin_implementation__ = plugin

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.transport.serial.factory": __plugin_implementation__.virtual_printer_factory,
        "octoprint.comm.transport.serial.additional_port_names": __plugin_implementation__.get_additional_port_names,
        "octoprint.filemanager.extension_tree": __plugin_implementation__.support_3mf_files,
        "octoprint.printer.sdcardupload": __plugin_implementation__.upload_to_sd,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }
