from __future__ import absolute_import, annotations
import os
import threading
import time
import flask
import datetime

import octoprint.printer
import octoprint.server
import octoprint.plugin
from octoprint.events import Events
import octoprint.settings
from octoprint.util import get_formatted_size, get_formatted_datetime, is_hidden_path
from octoprint.server.util.flask import no_firstrun_access
from octoprint.server.util.tornado import (
    LargeResponseHandler,
    path_validation_factory,
)
from octoprint.access.permissions import Permissions
from octoprint.logging.handlers import CleaningTimedRotatingFileHandler

from pybambu import BambuCloud

from urllib.parse import quote as urlquote
import logging.handlers

from octoprint_bambu_printer.bambu_virtual_printer import BambuVirtualPrinter
from .ftpsclient import IoTFTPSClient


class BambuPrintPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.BlueprintPlugin,
):
    _logger: logging.Logger
    _printer: octoprint.printer.PrinterInterface
    _settings: octoprint.settings.Settings
    _plugin_manager: octoprint.plugin.PluginManager

    def get_assets(self):
        return {"js": ["js/bambu_printer.js"]}

    def get_template_configs(self):
        return [
            {"type": "settings", "custom_bindings": True},
            {
                "type": "generic",
                "custom_bindings": True,
                "template": "bambu_timelapse.jinja2",
            },
        ]  # , {"type": "generic", "custom_bindings": True, "template": "bambu_printer.jinja2"}]

    def get_settings_defaults(self):
        return {
            "device_type": "X1C",
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
            "auth_token": "",
            "always_use_default_options": False,
        }

    def is_api_adminonly(self):
        return True

    def get_api_commands(self):
        return {"register": ["email", "password", "region", "auth_token"]}

    def on_api_command(self, command, data):
        if command == "register":
            if (
                "email" in data
                and "password" in data
                and "region" in data
                and "auth_token" in data
            ):
                self._logger.info(f"Registering user {data['email']}")
                bambu_cloud = BambuCloud(
                    data["region"], data["email"], data["password"], data["auth_token"]
                )
                bambu_cloud.login(data["region"], data["email"], data["password"])
                return flask.jsonify(
                    {
                        "auth_token": bambu_cloud.auth_token,
                        "username": bambu_cloud.username,
                    }
                )

    def on_event(self, event, payload):
        if event == Events.TRANSFER_DONE:
            self._printer.commands("M20 L T", force=True)

    def support_3mf_files(self):
        return {"machinecode": {"3mf": ["3mf"]}}

    def upload_to_sd(
        self,
        printer,
        filename,
        path,
        sd_upload_started,
        sd_upload_succeeded,
        sd_upload_failed,
        *args,
        **kwargs,
    ):
        self._logger.debug(f"Starting upload from {filename} to {filename}")
        sd_upload_started(filename, filename)

        def process():
            host = self._settings.get(["host"])
            access_code = self._settings.get(["access_code"])
            elapsed = time.monotonic()
            try:
                ftp = IoTFTPSClient(
                    f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
                )
                if ftp.upload_file(path, f"{filename}"):
                    elapsed = time.monotonic() - elapsed
                    sd_upload_succeeded(filename, filename, elapsed)
                    # remove local file after successful upload to Bambu
                    # self._file_manager.remove_file("local", filename)
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

    def get_template_vars(self):
        return {"plugin_version": self._plugin_version}

    def virtual_printer_factory(self, comm_instance, port, baudrate, read_timeout):
        if not port == "BAMBU":
            return None
        if (
            self._settings.get(["serial"]) == ""
            or self._settings.get(["host"]) == ""
            or self._settings.get(["access_code"]) == ""
        ):
            return None
        seriallog_handler = CleaningTimedRotatingFileHandler(
            self._settings.get_plugin_logfile_path(postfix="serial"),
            when="D",
            backupCount=3,
        )
        seriallog_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        seriallog_handler.setLevel(logging.DEBUG)

        serial_obj = BambuVirtualPrinter(
            self._settings,
            self._printer_profile_manager,
            data_folder=self.get_plugin_data_folder(),
            seriallog_handler=seriallog_handler,
            read_timeout=float(read_timeout),
            faked_baudrate=baudrate,
        )
        return serial_obj

    def get_additional_port_names(self, *args, **kwargs):
        if (
            self._settings.get(["serial"]) != ""
            and self._settings.get(["host"]) != ""
            and self._settings.get(["access_code"]) != ""
        ):
            return ["BAMBU"]
        else:
            return []

    def get_timelapse_file_list(self):
        if flask.request.path.startswith("/api/timelapse"):

            def process():
                host = self._settings.get(["host"])
                access_code = self._settings.get(["access_code"])
                return_file_list = []
                try:
                    ftp = IoTFTPSClient(
                        f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
                    )
                    if self._settings.get(["device_type"]) in ["X1", "X1C"]:
                        timelapse_file_list = ftp.list_files("timelapse/", ".mp4") or []
                    else:
                        timelapse_file_list = ftp.list_files("timelapse/", ".avi") or []
                    for entry in timelapse_file_list:
                        if entry.startswith("/"):
                            filename = entry[1:].replace("timelapse/", "")
                        else:
                            filename = entry.replace("timelapse/", "")
                        filesize = ftp.ftps_session.size(f"timelapse/{filename}")
                        date_str = ftp.ftps_session.sendcmd(
                            f"MDTM timelapse/{filename}"
                        ).replace("213 ", "")
                        filedate = (
                            datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S")
                            .replace(tzinfo=datetime.timezone.utc)
                            .timestamp()
                        )
                        return_file_list.append(
                            {
                                "bytes": filesize,
                                "date": get_formatted_datetime(
                                    datetime.datetime.fromtimestamp(filedate)
                                ),
                                "name": filename,
                                "size": get_formatted_size(filesize),
                                "thumbnail": "/plugin/bambu_printer/thumbnail/"
                                + filename.replace(".mp4", ".jpg").replace(
                                    ".avi", ".jpg"
                                ),
                                "timestamp": filedate,
                                "url": f"/plugin/bambu_printer/timelapse/{filename}",
                            }
                        )
                    self._plugin_manager.send_plugin_message(
                        self._identifier, {"files": return_file_list}
                    )
                except Exception as e:
                    self._logger.debug(f"Error getting timelapse files: {e}")

            thread = threading.Thread(target=process)
            thread.daemon = True
            thread.start()

    def _hook_octoprint_server_api_before_request(self, *args, **kwargs):
        return [self.get_timelapse_file_list]

    @octoprint.plugin.BlueprintPlugin.route("/timelapse/<filename>", methods=["GET"])
    @octoprint.server.util.flask.restricted_access
    @no_firstrun_access
    @Permissions.TIMELAPSE_DOWNLOAD.require(403)
    def downloadTimelapse(self, filename):
        dest_filename = os.path.join(self.get_plugin_data_folder(), filename)
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])
        if not os.path.exists(dest_filename):
            ftp = IoTFTPSClient(
                f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
            )
            download_result = ftp.download_file(
                source=f"timelapse/{filename}",
                dest=dest_filename,
            )
        return flask.redirect(
            "/plugin/bambu_printer/download/timelapse/" + urlquote(filename), code=302
        )

    @octoprint.plugin.BlueprintPlugin.route("/thumbnail/<filename>", methods=["GET"])
    @octoprint.server.util.flask.restricted_access
    @no_firstrun_access
    @Permissions.TIMELAPSE_DOWNLOAD.require(403)
    def downloadThumbnail(self, filename):
        dest_filename = os.path.join(self.get_plugin_data_folder(), filename)
        host = self._settings.get(["host"])
        access_code = self._settings.get(["access_code"])
        if not os.path.exists(dest_filename):
            ftp = IoTFTPSClient(
                f"{host}", 990, "bblp", f"{access_code}", ssl_implicit=True
            )
            download_result = ftp.download_file(
                source=f"timelapse/thumbnail/{filename}",
                dest=dest_filename,
            )
        return flask.redirect(
            "/plugin/bambu_printer/download/thumbnail/" + urlquote(filename), code=302
        )

    def is_blueprint_csrf_protected(self):
        return True

    def route_hook(self, server_routes, *args, **kwargs):
        return [
            (
                r"/download/timelapse/(.*)",
                LargeResponseHandler,
                {
                    "path": self.get_plugin_data_folder(),
                    "as_attachment": True,
                    "path_validation": path_validation_factory(
                        lambda path: not is_hidden_path(path), status_code=404
                    ),
                },
            ),
            (
                r"/download/thumbnail/(.*)",
                LargeResponseHandler,
                {
                    "path": self.get_plugin_data_folder(),
                    "as_attachment": True,
                    "path_validation": path_validation_factory(
                        lambda path: not is_hidden_path(path), status_code=404
                    ),
                },
            ),
        ]

    def get_update_information(self):
        return {
            "bambu_printer": {
                "displayName": "Bambu Printer",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "jneilliii",
                "repo": "OctoPrint-BambuPrinter",
                "current": self._plugin_version,
                "stable_branch": {
                    "name": "Stable",
                    "branch": "master",
                    "comittish": ["master"],
                },
                "prerelease_branches": [
                    {
                        "name": "Release Candidate",
                        "branch": "rc",
                        "comittish": ["rc", "master"],
                    }
                ],
                "pip": "https://github.com/jneilliii/OctoPrint-BambuPrinter/archive/{target_version}.zip",
            }
        }
