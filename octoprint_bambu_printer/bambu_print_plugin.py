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


    def _extract_and_parse_slice_info(self, local_filepath):
        """
        Extracts and parses the slice_info.config XML from a .3mf zip file.

        Args:
            local_filepath (str): The path to the local .3mf file.

        Returns:
            dict or None: A dictionary containing the parsed slice info, or None
                          if the file is not found or parsing fails.
        """
        parsed_slice_info = None
        self._logger.debug(f"Attempting to extract slice_info from {local_filepath}")
        try:
            # Check if the file is indeed a zip file before attempting to open as one
            if not zipfile.is_zipfile(local_filepath):
                 self._logger.error(f"File {local_filepath} is not a valid zip file (expected for .3mf). Cannot extract slice_info.")
                 return None

            with zipfile.ZipFile(local_filepath) as zipObj:
                self._logger.debug("Opened .3mf as zip.")
                # Read the slice_info.config content
                try:
                    with zipObj.open("Metadata/slice_info.config", "r") as config_data:
                        slice_info_content_bytes = config_data.read()
                        slice_info_content_str = slice_info_content_bytes.decode('utf-8')
                        self._logger.debug("Read slice_info.config content.")

                        # --- XML PARSING LOGIC ---
                        parsed_slice_info = {}
                        try:
                            root = ET.fromstring(slice_info_content_str)
                            self._logger.debug("Parsed slice_info.config XML.")

                            # Extract Header Items
                            header_element = root.find('header')
                            if header_element is not None:
                                parsed_slice_info['header'] = {item.get('key'): item.get('value') for item in header_element.findall('header_item') if item.get('key')}

                            # Extract Plate Data (Metadata, Filament List, Layer Lists, etc.)
                            plate_element = root.find('plate')
                            if plate_element is not None:
                                parsed_slice_info['plate'] = {}
                                # Extract Plate Metadata Attributes (index, extruder_type, etc.)
                                parsed_slice_info['plate']['metadata_attrs'] = plate_element.attrib
                                # Extract <metadata> key/value pairs
                                parsed_slice_info['plate']['metadata_items'] = {item.get('key'): item.get('value') for item in plate_element.findall('metadata') if item.get('key')}

                                # Extract Filament List
                                parsed_slice_info['plate']['filaments'] = []
                                for filament_element in plate_element.findall('filament'):
                                    filament_details = {
                                        'id': filament_element.get('id'),
                                        'tray_info_idx': filament_element.get('tray_info_idx'),
                                        'type': filament_element.get('type'),
                                        'color': filament_element.get('color'),
                                        'used_m': filament_element.get('used_m'),
                                        'used_g': filament_element.get('used_g'),
                                    }
                                    # Convert types if needed (int, float) - add error handling
                                    try:
                                        filament_details['id'] = int(filament_details['id']) if filament_details['id'] is not None else None
                                        filament_details['used_m'] = float(filament_details['used_m']) if filament_details['used_m'] is not None else 0.0
                                        filament_details['used_g'] = float(filament_details['used_g']) if filament_details['used_g'] is not None else 0.0
                                    except (ValueError, TypeError):
                                        self._logger.warn(f"Could not convert filament numeric values in slice_info for {os.path.basename(local_filepath)}")
                                        pass # Keep as string if conversion fails

                                    parsed_slice_info['plate']['filaments'].append(filament_details)

                                # Extract Layer Filament Lists
                                parsed_slice_info['plate']['layer_filament_lists'] = []
                                layer_filament_lists_element = plate_element.find('layer_filament_lists')
                                if layer_filament_lists_element is not None:
                                    for layer_list_element in layer_filament_lists_element.findall('layer_filament_list'):
                                         filament_ids_str = layer_list_element.get('filament_list')
                                         layer_ranges_str = layer_list_element.get('layer_ranges')

                                         # Parse filament IDs string (e.g., "0 1") into a list of integers
                                         filament_ids = [int(id.strip()) for id in filament_ids_str.split()] if filament_ids_str else []

                                         # Parse layer ranges string (e.g., "84 91") into a tuple of integers
                                         try:
                                             layer_range = tuple(int(r.strip()) for r in layer_ranges_str.split()) if layer_ranges_str else None
                                         except (ValueError, TypeError):
                                              self._logger.warn(f"Could not parse layer ranges in slice_info for {os.path.basename(local_filepath)}: {layer_ranges_str}")
                                              layer_range = None # Keep as None if parsing fails

                                         parsed_slice_info['plate']['layer_filament_lists'].append({
                                             'filament_ids': filament_ids,
                                             'layer_ranges': layer_range
                                         })

                                # TODO: Extract other sections like 'object', 'warning' if needed

                        except ET.ParseError as e:
                            self._logger.error(f"Error parsing slice_info.config XML for {local_filepath}: {e}")
                            # Optionally store the raw content as a fallback if parsing fails
                            parsed_slice_info = {"parse_error": str(e), "raw_content": slice_info_content_str}

                    # If XML parsing was successful, return the dictionary
                    if parsed_slice_info:
                        self._logger.debug(f"Successfully parsed slice_info for {os.path.basename(local_filepath)}")
                        return parsed_slice_info
                    else:
                         # Parsing failed but no exception was raised (e.g. ET.ParseError caught and handled)
                         return None # Ensure None is returned on parsing failure after catching ET.ParseError

                except KeyError:
                    self._logger.debug(f"slice_info.config not found in 3mf file: {local_filepath}")
                    return None # Return None if slice_info.config is not found within the zip
                except Exception as e:
                    self._logger.error(f"Unexpected error accessing or processing slice_info.config from 3mf file {local_filepath}: {e}")
                    return None # Return None for any other extraction/reading errors

        except Exception as e:
            self._logger.error(f"Error opening 3mf file {local_filepath} as zip: {e}")
            return None # Return None if the main zip file cannot be opened



    def _map_sd_file_to_local(self, sd_filename: str) -> str | None:
        """
        Maps a filename or path from the printer's SD card list (as seen in OctoPrint)
        to the corresponding local file path in OctoPrint's uploads folder.

        This is necessary because OctoPrint's SD card list might use 8.3 short names,
        while the local metadata is keyed by the original long filename.

        Args:
            sd_filename: The filename or path of the file as it appears in
                         OctoPrint's listing of the printer's SD card (e.g., "WINDOW~2.3MF").

        Returns:
            The path to the corresponding local file in OctoPrint's uploads
            folder (which is the original long filename, e.g., "window-IMG_20250510_194007_PLA_1h38m.3mf"),
            or None if no matching local file is found.
        """
        self._logger.debug(f"Attempting to map SD file '{sd_filename}' to local file.")

        # 1. Access the cached list of files on the SD card.
        # This list contains FileInfo objects with details about the SD card files.
        # Assuming self._project_files_view is the CachedFileView instance
        # in BambuPrintPlugin that manages the list of .3mf files on the SD card.
        if not hasattr(self, '_project_files_view') or self._project_files_view is None:
             self._logger.error("SD card project files view (_project_files_view) not available in BambuPrintPlugin. Cannot perform mapping.")
             return None

        # Get the cached FileInfo objects for all files in the SD card view.
        # This should trigger an update from the printer if the cache is stale,
        # but relying on get_all_cached_info is faster if you expect the cache is fresh.
        # Let's try updating first to ensure we have the latest list from the printer.
        self._project_files_view.update() # Ensure the cache is up-to-date
        sd_card_files_info = self._project_files_view.get_all_cached_info()

        # 2. Access the list of files in OctoPrint's local uploads folder.
        # This requires OctoPrint's core file manager, which is available in the plugin class.
        if not hasattr(self, '_file_manager') or self._file_manager is None:
             self._logger.error("OctoPrint file manager (_file_manager) not available in BambuPrintPlugin. Cannot perform mapping.")
             return None

        # Get the list of local files. The keys are paths relative to the uploads folder
        # (which are typically the original long filenames).
        # The values are dictionaries containing file details managed by OctoPrint.
        # We need both the key (the local path/long filename) and the file data
        # (which might contain the calculated local 8.3 DOS name).
        local_files_list = self._file_manager.list_files("local", collect=True) # collect=True ensures we get the full dict

        # --- 3. Implement the matching logic ---
        # We need to find an entry in local_files_list that corresponds to the sd_filename.
        # The matching needs to be robust because sd_filename could be an 8.3 name
        # or potentially a long name depending on the printer's M20 output.
        # We will compare the selected SD card file's information against the local files.

        matched_local_path = None

        # First, try to find the selected SD card file within our cached SD card list.
        # This gives us the FileInfo object for the SD entry, which has info like dosname, file_name, path (on SD).
        selected_sd_file_info: FileInfo | None = None

        # Normalize the input sd_filename for comparison (case-insensitive, strip leading/trailing slashes)
        normalized_sd_filename = sd_filename.strip("/").lower()

        for sd_info in sd_card_files_info:
            # Compare the input sd_filename against various names/paths in the SD cache
            if (sd_info.file_name.lower().strip("/") == normalized_sd_filename or
                sd_info.dosname.lower().strip("/") == normalized_sd_filename or
                sd_info.path.as_posix().lower().strip("/") == normalized_sd_filename):
                 selected_sd_file_info = sd_info
                 break # Found the matching SD card FileInfo in the cache


        if selected_sd_file_info is None:
             self._logger.debug(f"SD file '{sd_filename}' not found in the plugin's SD card cache.")
             return None

        self._logger.debug(f"Matched SD file in cache: {selected_sd_file_info.file_name} (DOS: {selected_sd_file_info.dosname}, Path: {selected_sd_file_info.path})")


        # Now, iterate through the local files list from OctoPrint's file manager
        # to find a match for the selected SD card file.
        # We will primarily compare based on the 8.3 DOS name, as this is often consistent
        # between the printer's report and OctoPrint's calculation for local files.
        # We can also add other checks if needed (like long name comparison or size).

        # Get the 8.3 DOS name of the selected SD card file for comparison
        sd_file_dos_name = selected_sd_file_info.dosname.lower().strip("/")

        for local_path, local_file_data in local_files_list.items():
             # local_path is the path relative to the uploads folder (usually the original long filename)
             local_file_name_long = os.path.basename(local_path)
             # Get the calculated 8.3 DOS name for the local file from OctoPrint's data
             local_file_dos_name = local_file_data.get("gcodeAnalysis", {}).get("dosFilename", "").lower().strip("/")
             local_size = local_file_data.get("size") # File size (can be used for additional check)

             # Comparison Strategy 1: Match by 8.3 DOS Name (Most reliable method)
             if sd_file_dos_name and local_file_dos_name and sd_file_dos_name == local_file_dos_name:
                  self._logger.debug(f"Matched local file '{local_file_name_long}' by 8.3 DOS name: {sd_file_dos_name}")
                  matched_local_path = local_path # Return the path relative to uploads (original long filename)
                  break # Found a match, exit loop

             # Comparison Strategy 2: Match by Original Long Name (if the SD list happens to provide it accurately)
             # This is less reliable if the printer always reports 8.3 names.
             elif selected_sd_file_info.file_name.lower().strip("/") == local_file_name_long.lower().strip("/"):
                  self._logger.debug(f"Matched local file '{local_file_name_long}' by long name.")
                  matched_local_path = local_path
                  break

             # Comparison Strategy 3: Match by Size (as an additional check, less unique)
             # Only use if size is reliably available in both SD FileInfo and local_file_data
             # elif selected_sd_file_info.size is not None and local_size is not None and selected_sd_file_info.size == local_size:
             #      self._logger.debug(f"Matched local file '{local_file_name_long}' by size.")
             #      matched_local_path = local_path
             #      break


        if matched_local_path:
             self._logger.debug(f"Successfully mapped SD file '{sd_filename}' to local path '{matched_local_path}'.")
             # The matched_local_path is the original long filename relative to the uploads folder.
             return matched_local_path
        else:
             self._logger.warning(f"Could not find a matching local file in OctoPrint's uploads for SD file '{sd_filename}'.")
             # This happens if the file wasn't uploaded via OctoPrint or mapping failed.
             return None

    # You will also implement _get_local_metadata_for_sd_file in this class later.
    # And move _get_metadata_from_uploads_root to this class.



    def _get_metadata_from_uploads_root(self) -> Dict[str, Any] | None:
        """
        Reads the entire content of the single .metadata.json file
        located in OctoPrint's uploads root folder.

        This is necessary because this OctoPrint instance consolidates
        all additional metadata into this single file, keyed by filename.

        Returns:
            A dictionary containing the entire additional metadata structure
            from the uploads root .metadata.json file, or None if the file
            doesn't exist or cannot be read/parsed.
        """
        # Get the base folder for uploads from settings.
        # Access settings via self._settings (available in the plugin class).
        if not hasattr(self, '_settings') or self._settings is None:
             self._logger.error("Settings instance (_settings) not available in BambuPrintPlugin. Cannot determine uploads path to read root metadata.")
             return None

        uploads_base_folder = self._settings.getBaseFolder("uploads")

        # The single metadata file is expected to be named .metadata.json
        # in the uploads root directory.
        metadata_file_path = os.path.join(uploads_base_folder, ".metadata.json")

        self._log.debug(f"Attempting to read consolidated metadata from: {metadata_file_path}")

        # Check if the consolidated metadata file exists
        if not os.path.exists(metadata_file_path):
            self._log.warning(f"Consolidated metadata file not found at {metadata_file_path}")
            return None

        try:
            # Read and parse the JSON content from the consolidated metadata file.
            with open(metadata_file_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
                self._log.debug(f"Successfully read consolidated metadata from {metadata_file_path}")
                # This dictionary loaded here contains entries for potentially ALL files in uploads,
                # keyed by their original filenames.
                return metadata
        except FileNotFoundError:
            # This check is somewhat redundant due to os.path.exists, but good practice
            self._log.error(f"Consolidated metadata file not found during read attempt (should not happen if os.path.exists passed): {metadata_file_path}")
            return None
        except json.JSONDecodeError as e:
            # This handles cases where the .metadata.json file exists but is corrupted or not valid JSON.
            self._log.error(f"Error decoding JSON from consolidated metadata file {metadata_file_path}: {e}")
            return None
        except Exception as e:
            # Catch any other unexpected errors during file reading.
            self._log.error(f"Unexpected error reading consolidated metadata file {metadata_file_path}: {e}")
            return None


    def _get_local_metadata_for_sd_file(self, sd_filename: str) -> Dict[str, Any] | None:
        """
        Retrieves the additional metadata for a file selected from the printer's
        SD card list by mapping it to its corresponding local file and reading
        the metadata from the consolidated .metadata.json file.

        Args:
            sd_filename: The filename or path of the file as it appears in
                         OctoPrint's listing of the printer's SD card.

        Returns:
            A dictionary containing the additional metadata (plate_data, slice_info, etc.)
            for the specific file, or None if mapping fails or metadata is not found.
        """
        self._logger.debug(f"Attempting to retrieve metadata for SD file '{sd_filename}'.")

        # Step 1: Map the SD card filename to the local file path (original long filename).
        original_long_filename = self._map_sd_file_to_local(sd_filename)

        if original_long_filename is None:
            self._logger.warning(f"Could not map SD file '{sd_filename}' to a local file. Cannot retrieve metadata.")
            return None

        # Step 2: Read the entire consolidated metadata dictionary from the uploads root.
        all_metadata_dict = self._get_metadata_from_uploads_root()

        if all_metadata_dict is None:
             self._logger.error("Failed to read the consolidated metadata dictionary from uploads root.")
             return None

        # Step 3: Use the original long filename as the key to look up the specific file's metadata.
        # The structure is { "original_long_filename": { "plugin_id": { "plate_data": {...}, ...}, ...} }
        file_specific_metadata = all_metadata_dict.get(original_long_filename)

        if file_specific_metadata is None:
            self._logger.warning(f"No entry found in consolidated metadata dictionary for original filename: '{original_long_filename}'. Metadata may not have been saved during upload.")
            # This could happen if the file was uploaded but on_event failed, or it was added manually to .metadata.json without plugin processing.
            return None

        # Access the dictionary containing *only* this plugin's metadata (plate_data, slice_info, thumbnail_src, etc.).
        # Use .get() with an empty dictionary default for safety in case the plugin's metadata is missing entirely for this file.
        # Ensure the plugin's identifier (_identifier) is available in the plugin class.
        if not hasattr(self, '_identifier') or self._identifier is None:
             self._logger.error("Plugin identifier (_identifier) not available in BambuPrintPlugin.")
             return None

        plugin_identifier = self._identifier # Get the plugin's identifier

        plugin_metadata = file_specific_metadata.get(plugin_identifier, {})

        # The 'plugin_metadata' dictionary should now contain the plate_data and slice_info.
        self._logger.debug(f"Successfully retrieved plugin metadata for '{original_long_filename}'.")

        # Return the dictionary containing the plugin's metadata for this file.
        # The caller (IdleState) will then access plate_data and slice_info from this dictionary.
        return plugin_metadata




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
