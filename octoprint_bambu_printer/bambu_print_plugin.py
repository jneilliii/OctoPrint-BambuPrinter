from __future__ import absolute_import, annotations

import json
from pathlib import Path
import threading
from time import perf_counter
from contextlib import contextmanager
import flask
import logging.handlers
from urllib.parse import quote as urlquote
import os
import zipfile

import octoprint.printer
import octoprint.server
import octoprint.plugin
from octoprint.events import Events
import octoprint.settings
from octoprint.settings import valid_boolean_trues
from octoprint.util import is_hidden_path
from octoprint.server.util.flask import no_firstrun_access
from octoprint.server.util.tornado import (
    LargeResponseHandler,
    path_validation_factory,
)
from.LargeResponseHandlerWithFallback import LargeResponseHandlerWithFallback
from octoprint.access.permissions import Permissions
from octoprint.logging.handlers import CleaningTimedRotatingFileHandler

from octoprint_bambu_printer.printer.file_system.cached_file_view import CachedFileView
from octoprint_bambu_printer.printer.pybambu import BambuCloud

import xml.etree.ElementTree as ET

from octoprint_bambu_printer.printer.file_system.remote_sd_card_file_list import (
    RemoteSDCardFileList,
)

from typing import Dict, Any

from octoprint_bambu_printer.printer.file_system.bambu_timelapse_file_info import (
    BambuTimelapseFileInfo,
    FileInfo
)
from octoprint_bambu_printer.printer.bambu_virtual_printer import BambuVirtualPrinter
import shutil


@contextmanager
def measure_elapsed():
    start = perf_counter()

    def _get_elapsed():
        return perf_counter() - start

    yield _get_elapsed
    print(f"Total elapsed: {_get_elapsed()}")


class BambuPrintPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.StartupPlugin,
):
    _logger: logging.Logger
    _plugin_manager: octoprint.plugin.PluginManager
    _bambu_file_system: RemoteSDCardFileList
    _timelapse_files_view: CachedFileView
    _project_files_view: CachedFileView
    _bambu_cloud: None

    def on_settings_initialized(self):
        self._bambu_file_system = RemoteSDCardFileList(self._settings)
        self._timelapse_files_view = CachedFileView(self._bambu_file_system)
        self._project_files_view = CachedFileView(self._bambu_file_system, on_update=self._update_file_list)
        self.valid_boolean_trues = ["True", "true", "1", "yes"]  # Must match payload case!

        if self._settings.get(["device_type"]) in ["X1", "X1C"]:
            self._timelapse_files_view.with_filter("timelapse/", ".mp4")
        else:
            self._timelapse_files_view.with_filter("timelapse/", ".avi")

    def _update_file_list(self):
        self._printer.commands("M20 L T", force=True)

    def get_assets(self):
        return {"js": ["js/jquery-ui.min.js", "js/knockout-sortable.1.2.0.js", "js/bambu_printer.js"],
                "css": ["css/bambu_printer.css"]
                }

    def on_after_startup(self):
        if not os.path.exists(os.path.join(self.get_plugin_data_folder(), "thumbs", "no_thumb.png")):
            self._logger.info("Creating no_thumb.png")
            shutil.copy(os.path.join(self._basefolder, "static", "img", "no_thumb.png"), os.path.join(self.get_plugin_data_folder(), "thumbs"))

    def get_template_configs(self):
        return [
            {"type": "settings", "custom_bindings": True},
            {
                "type": "generic",
                "custom_bindings": True,
                "template": "bambu_timelapse.jinja2",
            },
            {"type": "generic", "custom_bindings": True, "template": "bambu_printer.jinja2"}]

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
            "layer_inspect": False,
            "use_ams": False,
            "local_mqtt": True,
            "region": "",
            "email": "",
            "auth_token": "",
            "always_use_default_options": False,
            "ams_data": [],
            "ams_mapping": [],
            "ams_current_tray": 255,
        }

    def on_settings_save(self, data):
        if data.get("local_mqtt", False) is True:
            data["auth_token"] = ""
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

    def is_api_adminonly(self):
        return True

    def get_api_commands(self):
        return {"register": ["email", "password", "region", "auth_token"],
                "verify": ["auth_type", "password"]}

    def on_api_command(self, command, data):
        if command == "register":
            if (
                "email" in data
                and "password" in data
                and "region" in data
                and "auth_token" in data
            ):
                self._logger.info(f"Registering user {data['email']}")
                self._bambu_cloud = BambuCloud(data["region"], data["email"], data["password"], data["auth_token"])
                auth_response = self._bambu_cloud.login(data["region"], data["email"], data["password"])
                return flask.jsonify(
                    {
                        "auth_response": auth_response,
                    }
                )
        elif command == "verify":
            auth_response = None
            if (
                "auth_type" in data
                and "password" in data
                and self._bambu_cloud is not None
            ):
                self._logger.info(f"Verifying user {self._bambu_cloud._email}")
                if data["auth_type"] == "verifyCode":
                    auth_response = self._bambu_cloud.login_with_verification_code(data["password"])
                elif data["auth_type"] == "tfa":
                    auth_response = self._bambu_cloud.login_with_2fa_code(data["password"])
                else:
                    self._logger.warning(f"Unknown verification type: {data['auth_type']}")

                if auth_response == "success":
                    return flask.jsonify(
                        {
                            "auth_token": self._bambu_cloud.auth_token,
                            "username": self._bambu_cloud.username
                        }
                    )
                else:
                    self._logger.info(f"Error verifying: {auth_response}")
                    return flask.jsonify(
                        {
                            "error": "Unable to verify"
                        }
                    )

    def _parse_slice_info_config(self, xml_content):
        """
        Parses the XML content of slice_info.config into a dictionary.
        Maps attributes to dictionary keys, handles lists for repeated elements.
        """
        parsed_data = {}
        try:
            root = ET.fromstring(xml_content)

            # Parse Header section
            header_elem = root.find('header')
            if header_elem is not None:
                parsed_data['header'] = {}
                for item in header_elem.findall('header_item'):
                    key = item.get('key')
                    value = item.get('value')
                    if key is not None: # Ensure key exists
                        parsed_data['header'][key] = value

            # Parse Plate section
            plate_elem = root.find('plate')
            if plate_elem is not None:
                parsed_data['plate'] = {}

                # Collect metadata items
                metadata_items = plate_elem.findall('metadata')
                if metadata_items:
                    parsed_data['plate']['metadata'] = {}
                    for item in metadata_items:
                        key = item.get('key')
                        value = item.get('value')
                        if key is not None:
                            parsed_data['plate']['metadata'][key] = value

                # Collect object items (assuming multiple objects are possible)
                object_items = plate_elem.findall('object')
                if object_items:
                    parsed_data['plate']['objects'] = []
                    for item in object_items:
                        # Store all attributes for the object element
                        parsed_data['plate']['objects'].append(item.attrib)

                # Collect filament items (assuming multiple filaments are possible)
                filament_items = plate_elem.findall('filament')
                if filament_items:
                    parsed_data['plate']['filaments'] = []
                    for item in filament_items:
                        # Store all attributes for the filament element
                        parsed_data['plate']['filaments'].append(item.attrib)

                # Add any other direct children of plate if necessary in the future
                # For now, metadata, object, and filament cover the example.

            return parsed_data

        except ET.ParseError as e:
            self._logger.error(f"Failed to parse slice_info.config XML: {e}")
            return None # Indicate parsing failure
        except Exception as e:
            self._logger.error(f"An unexpected error occurred during slice_info.config parsing: {e}")
            return None

    def on_event(self, event, payload):
        if event == Events.TRANSFER_DONE:
            self._printer.commands("M20 L T", force=True)
        elif event == Events.FILE_ADDED:
            # ... (your existing FILE_ADDED logic remains the same)
            if payload["operation"] == "add" and "3mf" in payload["type"]:
                file_container = os.path.join(self._settings.getBaseFolder("uploads"), payload["path"])
                if os.path.exists(file_container):
                    png_folder_path = os.path.join(self.get_plugin_data_folder(), "thumbs")
                    if not os.path.exists(png_folder_path):
                        os.makedirs(png_folder_path)
                    png_file_name = os.path.join(png_folder_path, payload["name"] + ".png")

                    try:
                         with zipfile.ZipFile(file_container) as zipObj:
                             # ... (your existing 3mf extraction logic remains the same)
                             # --- Existing PNG and Plate JSON Extraction ---
                            try:
                                # extract thumbnail
                                zipInfo = zipObj.getinfo("Metadata/plate_1.png")
                                zipInfo.filename = os.path.basename(png_file_name)
                                zipObj.extract(zipInfo, png_folder_path)
                                if os.path.exists(png_file_name):
                                    thumb_url = f"/plugin/bambu_printer/download/thumbs/{payload['name']}.png"
                                    self._file_manager.set_additional_metadata("local", payload["path"], "thumbnail_src",
                                                                               self._identifier, overwrite=True)
                                    self._file_manager.set_additional_metadata("local", payload["path"], "thumbnail",
                                                                               thumb_url, overwrite=True)

                                # extract plate data
                                with zipObj.open("Metadata/plate_1.json", "r") as json_data:
                                    plate_data = json.load(json_data)
                                    if plate_data:
                                        # TODO: once sdcard has a true storage interface change from local
                                        self._file_manager.set_additional_metadata("local", payload["path"], "plate_data",
                                                                                   plate_data, overwrite=True)
                            except KeyError:
                                # Original file not found error log
                                self._logger.info(f"unable to extract from 3mf file: {file_container}")
                            except Exception as e:
                                # Catch other errors during the original extraction
                                self._logger.error(f"An error occurred during existing 3mf extraction (png/json) for {file_container}: {e}")


                            # --- New Slice Info Config Extraction, Parsing, and Saving ---
                            try:
                                # Access slice_info.config from the zip
                                with zipObj.open("Metadata/slice_info.config", "r") as config_file:
                                    # Read and decode the content (XML is text)
                                    config_content = config_file.read().decode('utf-8')

                                    # Parse the XML content using the helper function
                                    parsed_config = self._parse_slice_info_config(config_content)

                                    if parsed_config: # Only save if parsing was successful
                                        # Construct the output file path in the uploads folder
                                        # Using a leading dot for a hidden file, as per your example
                                        upload_folder = self._settings.getBaseFolder("uploads")
                                        output_json_path = os.path.join(upload_folder, "." + payload["name"] + ".json")

                                        # Save the parsed data to a JSON file
                                        with open(output_json_path, "w") as f:
                                            json.dump(parsed_config, f, indent=4) # Use indent for readability

                                        self._logger.info(f"Successfully extracted, parsed, and saved slice_info.config for {payload['name']} to {output_json_path}")
                                    else:
                                         # _parse_slice_info_config logs specific errors if parsing failed
                                         self._logger.warning(f"Parsing of slice_info.config failed for {payload['name']}")

                            except KeyError:
                                # Log if slice_info.config is not found
                                self._logger.info(f"Metadata/slice_info.config not found in {file_container}")
                            except Exception as e:
                                # Catch any other exceptions during config processing (parsing, writing)
                                self._logger.error(f"An error occurred during slice_info.config processing for {file_container}: {e}")

                    except zipfile.BadZipFile:
                         self._logger.error(f"File is not a valid zip file: {file_container}")
                    except Exception as e:
                         self._logger.error(f"An unexpected error occurred while processing 3mf file {file_container}: {e}")


        elif event == Events.UPLOAD:
            # Check if the target is local or sdcard
            if payload["target"] in ["local", "sdcard"]:
                # Construct the full path to the file in OctoPrint's uploads folder
                path = os.path.join(self._settings.getBaseFolder("uploads"), payload["path"])

                filename = payload["name"]

                # Check if the file actually exists at the expected path before trying to upload
                if os.path.exists(path):
                    # Optional: Add a check here if you ONLY want to upload .3mf files via FTP
                    if filename.lower().endswith(".3mf"):
                        with self._bambu_file_system.get_ftps_client() as ftp:
                            if ftp.upload_file(path, filename):
                                self._logger.info(f"Successfully FTP uploaded {filename} from {path}")

                                # *** Add this block to refresh the SD card list after an SD card upload ***
                                if payload["target"] == "sdcard":
                                     self._logger.info("Triggering SD card file list refresh (M20)")
                                     self._printer.commands("M20")
                                # ***********************************************************************

                                # Reintroduce the logic to select and potentially print if the 'print' flag is true
                                if payload.get("print") in valid_boolean_trues:
                                    # Assuming the file is uploaded to the root of the printer's SD card with the same name
                                    printer_file_path = filename # Adjust if files are uploaded to subfolders on the printer SD

                                    self._logger.info(f"Attempting to select and print file on printer: {printer_file_path}")
                                    # The select_file command needs the path on the printer's filesystem
                                    # The second argument `True` means it's an SD file
                                    self._printer.select_file(printer_file_path, True, printAfterSelect=True) # printAfterSelect should be True here

                    else:
                        self._logger.info(f"Skipping FTP upload for non-.3mf file: {filename} uploaded to {payload['target']}")
                else:
                     self._logger.error(f"Uploaded file not found at expected path: {path}")
            else:
                self._logger.info(f"Upload target is not local or sdcard: {payload.get('target')}. Skipping FTP upload.")

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
            with measure_elapsed() as get_elapsed:
                try:
                    with self._bambu_file_system.get_ftps_client() as ftp:
                        existing_files = ftp.list_files()

                        # Get base name and extension
                        base_name, ext = os.path.splitext(filename)
                        prefix = base_name[:6].lower()

                        # Look for similar files that might conflict
                        existing_filenames = [os.path.basename(f["name"]).lower() for f in existing_files]
                        similar_files = [f for f in existing_filenames if f.startswith(prefix) and f.endswith(ext.lower())]

                        # If exact name exists, delete it (safe overwrite)
                        file_to_delete = next((Path(f["name"]) for f in existing_files if os.path.basename(f["name"]) == filename), None)
                        if file_to_delete:
                            self._logger.info(f"File '{filename}' already exists on SD card. Deleting '{file_to_delete}' before upload.")
                            self._bambu_file_system.delete_file(file_to_delete)

                        # Handle short name collision (e.g., cube_p~1.3mf)
                        if filename.lower() in existing_filenames:
                            new_index = 1
                            while True:
                                new_filename = f"{prefix}_{new_index}{ext}"
                                if new_filename not in existing_filenames:
                                    filename = new_filename
                                    break
                                new_index += 1
                            self._logger.info(f"Filename collision detected. Uploading as '{filename}' instead.")

                        # Upload file
                        if ftp.upload_file(path, filename):
                            sd_upload_succeeded(filename, filename, get_elapsed())
                            # Refresh file list
                            self.refresh_file_list()  # Trigger file list refresh after upload
                        else:
                            raise Exception("upload failed")
                except Exception as e:
                    sd_upload_failed(filename, filename, get_elapsed())
                    self._logger.exception("Upload failed with exception", exc_info=e)

        thread = threading.Thread(target=process)
        thread.daemon = True
        thread.start()
        return filename


    def refresh_file_list(self):
        """Method to trigger a refresh of the file list."""
        self._logger.debug("Refreshing file list.")
        # Assuming you have access to OctoPrint API to trigger refresh, you can use:
        self._printer.commands("M20")  # This command triggers a refresh of the SD card file list

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
            serial_log_handler=seriallog_handler,
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
                return_file_list = []
                for file_info in self._timelapse_files_view.get_all_info():
                    timelapse_info = BambuTimelapseFileInfo.from_file_info(file_info)
                    return_file_list.append(timelapse_info.to_dict())
                self._plugin_manager.send_plugin_message(
                    self._identifier, {"files": return_file_list}
                )

            thread = threading.Thread(target=process)
            thread.daemon = True
            thread.start()

    def _hook_octoprint_server_api_before_request(self, *args, **kwargs):
        return [self.get_timelapse_file_list]

    def _download_file(self, file_name: str, source_path: str):
        destination = Path(self.get_plugin_data_folder()) / file_name
        if destination.exists():
            return destination

        with self._bambu_file_system.get_ftps_client() as ftp:
            ftp.download_file(
                source=(Path(source_path) / file_name).as_posix(),
                dest=destination.as_posix(),
            )
        return destination

    @octoprint.plugin.BlueprintPlugin.route("/timelapse/<filename>", methods=["GET"])
    @octoprint.server.util.flask.restricted_access
    @no_firstrun_access
    @Permissions.TIMELAPSE_DOWNLOAD.require(403)
    def downloadTimelapse(self, filename):
        self._download_file(filename, "timelapse/")
        return flask.redirect(
            "/plugin/bambu_printer/download/timelapse/" + urlquote(filename), code=302
        )

    @octoprint.plugin.BlueprintPlugin.route("/thumbnail/<filename>", methods=["GET"])
    @octoprint.server.util.flask.restricted_access
    @no_firstrun_access
    @Permissions.TIMELAPSE_DOWNLOAD.require(403)
    def downloadThumbnail(self, filename):
        self._download_file(filename, "timelapse/thumbnail/")
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
            (
                r"/download/thumbs/(.*)",
                LargeResponseHandlerWithFallback,
                {
                    "path": os.path.join(self.get_plugin_data_folder(), "thumbs"),
                    "default_filename": "no_thumb.png",
                    "allow_client_caching": False,
                    # "as_attachment": True,
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
