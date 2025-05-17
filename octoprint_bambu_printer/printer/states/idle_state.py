from __future__ import annotations

import os
import json
import time # Import the time module

# Assume these are correctly imported from your plugin's structure
from octoprint_bambu_printer.printer.file_system.file_info import FileInfo
from octoprint_bambu_printer.printer.states.a_printer_state import APrinterState

# Assume other necessary components are available via 'self', e.g.:
# self._log (from APrinterState)
# self._printer (from APrinterState) # self._printer is likely the main plugin instance
# self._settings (from self._printer)
# Assume valid_boolean_trues is defined elsewhere and accessible


class IdleState(APrinterState):
    """
    Represents the idle state of the printer.
    Handles initiating new print jobs.
    """

    # --- Helper Function to Read .metadata.json (for hash) ---
    def _read_all_metadata_json(self) -> dict | None:
        """
        Reads the local .metadata.json file containing metadata for all files
        in the OctoPrint uploads folder. Used primarily to get the file hash.

        Returns:
            dict | None: The parsed JSON content as a dictionary (keyed by filename),
                         or None if the file is not found, cannot be read, or
                         is not valid JSON or not a dictionary.
        """
        try:
            local_uploads_dir = self._printer._settings.getBaseFolder("uploads")
        except Exception as e:
            self._log.error(f"Could not get OctoPrint uploads base folder: {e}. Cannot locate .metadata.json.")
            return None

        json_local_filename = ".metadata.json"
        json_full_local_path = os.path.join(local_uploads_dir, json_local_filename)


        self._log.debug(f"Looking for local metadata JSON at: {json_full_local_path}")

        if not os.path.exists(json_full_local_path):
            self._log.warning(f"Local .metadata.json file not found at {json_full_local_path}.")
            return None

        try:
            with open(json_full_local_path, 'r') as f:
                metadata = json.load(f)
                if isinstance(metadata, dict):
                    self._log.debug(f"Successfully read and parsed JSON from {json_full_local_path}.")
                    return metadata
                else:
                    self._log.error(f".metadata.json file {json_full_local_path} content is not a dictionary.")
                    return None
        except json.JSONDecodeError as e:
            self._log.error(f"Error decoding JSON from {json_full_local_path}: {e}")
            return None
        except Exception as e:
            self._log.error(f"Error reading local metadata file {json_full_local_path}: {e}")
            return None

    # --- Helper Function to Read file-specific metadata JSON (for filaments) ---
    def _read_file_metadata_json(self, filename_with_json_extension: str) -> dict | None:
        """
        Reads a specific local JSON file named after the full .3mf filename + .json
        in the OctoPrint uploads folder. Used to get sliced filament details.

        Args:
            filename_with_json_extension (str): The full name of the JSON file
                                                (e.g., ".Cone_PLA_20m9s.3mf.json").

        Returns:
            dict | None: The parsed JSON content as a dictionary, or None if the
                         file is not found, cannot be read, or is not valid JSON.
        """
        try:
            local_uploads_dir = self._printer._settings.getBaseFolder("uploads")
        except Exception as e:
            self._log.error(f"Could not get OctoPrint uploads base folder: {e}. Cannot locate file-specific metadata JSON.")
            return None

        json_full_local_path = os.path.join(local_uploads_dir, filename_with_json_extension)

        self._log.debug(f"Looking for file-specific metadata JSON at: {json_full_local_path}")

        if not os.path.exists(json_full_local_path):
            self._log.warning(f"File-specific metadata JSON file not found at {json_full_local_path}.")
            return None

        try:
            with open(json_full_local_path, 'r') as f:
                metadata = json.load(f)
                self._log.debug(f"Successfully read and parsed JSON from {json_full_local_path}.")
                return metadata
        except json.JSONDecodeError as e:
            self._log.error(f"Error decoding JSON from {json_full_local_path}: {e}")
            return None
        except Exception as e:
            self._log.error(f"Error reading file-specific metadata file {json_full_local_path}: {e}")
            return None

    # --- End of Helper Functions ---


    # --- Function to generate the AMS mapping (now a method) ---
    def generate_ams_mapping(self, sliced_metadata_for_file: dict | None, ams_data: list | None, log=None) -> list:
        """
        Generates the ams_mapping list for an MQTT print command based on
        sliced file metadata (for the specific file) and physical AMS data.

        It iterates through the filaments listed in the sliced file's metadata.
        For each sliced filament, it searches for a matching physical AMS tray
        based on filament type and unique index (tray_info_idx/idx).
        The order of the ams_mapping list elements corresponds directly to the
        order of filaments in the sliced metadata's 'filaments' list.
        If a physical match is found for a sliced filament, the corresponding
        global AMS tray ID is used in the mapping. If no physical match is found, -1 is used.

        Args:
            self: The instance of the class.
            sliced_metadata_for_file (dict | None): The parsed JSON metadata for the *specific*
                                                    .3mf file, expected to contain a 'plate' key
                                                    with a 'filaments' list.
            ams_data (list | None): The physical AMS status data, a list of AMS units,
                                    each containing a 'tray' list. Expected structure
                                    is like [{'tray': [...]}, {'tray': [...]}, ...].
            log: An optional logger object. If None, uses self._log.

        Returns:
            list: A list of integers representing the ams_mapping. Returns [] if inputs
                  are invalid or if there are no filaments in the sliced metadata.
                  Returns [-1, -1, ...] for each sliced filament if AMS data is missing.
        """
        current_log = log if log is not None else self._log

        ams_mapping = []

        # Now uses the metadata specifically for the selected file
        if not sliced_metadata_for_file:
            current_log.warning("No sliced metadata provided for the specific file. Cannot generate ams_mapping.")
            return []

        # Get the list of filaments from the sliced metadata's 'plate' section
        # This list's order determines the order of the ams_mapping
        # This path ('plate' -> 'filaments') is expected to be found in the file-specific JSON
        sliced_filaments = sliced_metadata_for_file.get("plate", {}).get("filaments", [])

        if not sliced_filaments:
            current_log.info("No filaments found in sliced metadata for the file. Returning empty ams_mapping.")
            # The problem is here: the path above failed to find the filaments.
            return []

        # Check for valid AMS data structure
        if not ams_data or not isinstance(ams_data, list) or not ams_data: # Ensure ams_data is not an empty list
            current_log.warning("No physical AMS data provided or data is invalid. Cannot match sliced filaments to physical trays.")
            current_log.warning(f"Returning ams_mapping of [-1] * {len(sliced_filaments)}.")
            return [-1] * len(sliced_filaments)

        current_log.debug(f"Processing sliced filaments ({len(sliced_filaments)}) to generate AMS mapping.")

        # Iterate through each filament defined in the sliced file (.3mf)
        # The index here corresponds to the position in the ams_mapping list
        for sliced_filament_index, sliced_filament in enumerate(sliced_filaments):
            matched_tray_id = -1 # Default to -1 (no physical match)

            # Extract key properties from the sliced filament definition
            # This assumes the filament object has 'type' and 'idx' keys
            sliced_type = sliced_filament.get('type', '').lower()
            sliced_idx = sliced_filament.get('tray_info_idx', '') # Unique ID from the spool/slicer

            if not sliced_type:
                current_log.warning(f"Sliced filament at index {sliced_filament_index} missing type or type is empty. Cannot match. Appending -1.")
                ams_mapping.append(-1)
                continue # Skip to next sliced filament

            # Iterate through each physical AMS unit and its trays to find a match
            match_found = False
            for ams_unit_index, ams_unit in enumerate(ams_data):
                # Ensure ams_unit is a dictionary and has 'tray' key
                if not isinstance(ams_unit, dict) or 'tray' not in ams_unit:
                    current_log.warning(f"AMS unit {ams_unit_index} is not a dictionary or missing 'tray' data. Skipping.")
                    continue

                physical_trays = ams_unit.get('tray', [])
                if not isinstance(physical_trays, list):
                     current_log.warning(f"AMS unit {ams_unit_index} has invalid 'tray' data (not a list). Skipping.")
                     continue # Skip this AMS unit if tray data is malformed

                for slot_index, physical_tray in enumerate(physical_trays): # slot_index is the correct variable name here
                    # Ensure physical_tray is a dictionary before using .get()
                    if not isinstance(physical_tray, dict):
                         # Corrected variable name from physical_tray_index to slot_index
                         current_log.warning(f"AMS unit {ams_unit_index}, tray {slot_index} has invalid data format (not a dictionary). Skipping.")
                         continue


                    physical_type = physical_tray.get('type', '').lower()
                    physical_idx = physical_tray.get('idx', '') # Unique ID from the physical tag/spool
                    is_empty = physical_tray.get('empty', True)

                    # Match criteria: Physical slot is NOT empty AND
                    # (Physical Type matches Sliced Type AND Physical Index matches Sliced Index)
                    # This matching logic assumes sliced_filament has 'type' and 'idx'
                    if not is_empty and physical_type == sliced_type and physical_idx and physical_idx == sliced_idx:
                         # Calculate the global AMS tray ID (0-3 for AMS 1, 4-7 for AMS 2, etc.)
                         # Corrected variable name from physical_tray_index to slot_index
                         matched_tray_id = ams_unit_index * 4 + slot_index
                         current_log.debug(f"Matched sliced filament index {sliced_filament_index} (type={sliced_type}, idx={sliced_idx}) to physical AMS Unit {ams_unit_index} Tray {slot_index} (Global ID: {matched_tray_id}).")
                         match_found = True
                         break # Found the match for this sliced filament, exit inner tray loop
                if match_found:
                    break # Found the match, exit outer AMS unit loop

            # After checking all physical trays, append the result for this sliced filament
            # matched_tray_id will be the physical ID if a match was found, otherwise it remains -1.
            ams_mapping.append(matched_tray_id)

        return ams_mapping


    def report_physical_ams_status(self):
        """
        Reports the real-time physical AMS status using cached data from self._printer.ams_data.
        Outputs parsed data to the OctoPrint terminal or log.
        """
        self._log.debug("Reporting physical AMS status (refined parse).")

        # Access the cached ams_data from the main plugin instance
        # Assuming the main plugin instance (self._printer) has an ams_data attribute
        # Use getattr for safety in case the attribute doesn't exist
        cached_ams_data = getattr(self._printer, 'ams_data', None)
        self._log.info("Reading cached AMS data from self._printer.ams_data for refined parsing.")
        self._log.debug(f"REFINED_PARSE: Content of self._printer.ams_data: {cached_ams_data}")

        if not cached_ams_data or not isinstance(cached_ams_data, list):
            # Updated message to reflect where the data is expected to be
            # Corrected to call sendIO via self._printer
            self._printer.sendIO("echo: No physical AMS tray data available in cache (self._printer.ams_data is empty or malformed).\n")
        else:
            # Updated message header
            # Corrected to call sendIO via self._printer
            self._printer.sendIO("echo: --- Physical AMS Status (Parsed from self._printer.ams_data) ---\n")

            try:
                # Using repr as in your original, but handling potential errors
                raw_data_string = "Error representing raw data"
                try:
                     raw_data_string = repr(cached_ams_data)
                except Exception:
                     # Fallback or simplified representation if repr fails
                     try:
                         raw_data_string = json.dumps(cached_ams_data, indent=2)
                     except Exception:
                         raw_data_string = f"Could not represent data: {type(cached_ams_data).__name__}"


                # Updated message to reflect where the data is expected to be
                # Corrected to call sendIO via self._printer
                self._printer.sendIO(f"echo: RAW self._printer.ams_data content: {raw_data_string}\n")
                self._log.debug("REFINED_PARSE: Sent raw data content to terminal.")
            except Exception as e:
                self._log.error(f"REFINED_PARSE: Error outputting raw data: {type(e).__name__}: {e}", exc_info=True)
                # Corrected to call sendIO via self._printer
                self._printer.sendIO(f"echo: Error outputting raw AMS data: {e}\n")

            parsed_ams_trays = []

            try:
                for unit_index, ams_unit_container in enumerate(cached_ams_data): # Uses cached_ams_data
                    self._log.debug(f"REFINED_PARSE: Processing AMS unit container {unit_index}. Content: {ams_unit_container}")
                    # Added safety checks using .get() and explicit type checks
                    if isinstance(ams_unit_container, dict) and 'tray' in ams_unit_container and isinstance(ams_unit_container.get('tray'), list):
                        ams_unit_trays = ams_unit_container.get('tray')
                        self._log.debug(f"REFINED_PARSE: Found 'tray' list for unit {unit_index}. Number of trays: {len(ams_unit_trays)}")

                        for slot_index, tray in enumerate(ams_unit_trays): # Uses slot_index
                            self._log.debug(f"REFINED_PARSE: Processing tray {slot_index} in unit {unit_index}. Content: {tray}")
                            # Added safety checks for tray dictionary and key presence
                            if tray and isinstance(tray, dict) and 'type' in tray and 'color' in tray:
                                material = tray.get("type", "Unknown")
                                color = tray.get("color", "00000000")
                                global_id = (unit_index * 4) + slot_index
                                # Added isinstance(color, str) check before slicing
                                processed_color = color[:6] if color and isinstance(color, str) and len(color) >= 6 else "000000"

                                parsed_ams_trays.append({
                                    'global_id': global_id,
                                    'type': material,
                                    'color': processed_color,
                                    'unit': unit_index,
                                    'slot': slot_index
                                })

                                # Corrected variable name from physical_tray_index to slot_index
                                self._log.debug(f"REFINED_PARSE: Parsed tray Unit {unit_index}, Slot {slot_index}. Global ID: {global_id}, Type: {material}, Color: #{processed_color}")
                            else:
                                # Corrected variable name from physical_tray_index to slot_index
                                self._log.debug(f"REFINED_PARSE: Skipping malformed tray data (not dict or missing type/color) at Unit {unit_index}, Slot {slot_index}.")
                    else:
                        self._log.debug(f"REFINED_PARSE: Skipping malformed AMS unit container data (not dict or missing 'tray' list) at index {unit_index}.")
            except Exception as e:
                self._log.error(f"REFINED_PARSE: Error parsing AMS data: {type(e).__name__}: {e}", exc_info=True)
                # Corrected to call sendIO via self._printer
                self._printer.sendIO(f"echo: Error parsing cached AMS data: {e}\n")
                parsed_ams_trays = [] # Clear partial results on error

            if parsed_ams_trays:
                for tray_data in parsed_ams_trays:
                    output_line = (
                        f"echo: AMS Unit {tray_data.get('unit', '?')}, Slot {tray_data.get('slot', '?')}"
                        f" (Global ID: {tray_data.get('global_id', '?')})"
                        f" - Type: {tray_data.get('type', 'Unknown')}, Color: #{tray_data.get('color', '000000')}\n"
                    )
                    # Corrected to call sendIO via self._printer
                    self._printer.sendIO(output_line)
                    self._log.debug(f"REFINED_PARSE: Output sent for Global ID {tray_data.get('global_id', '?')}.")
            else:
                # Modified this message to be less alarming if parsing resulted in empty list but raw data was shown
                # Corrected to call sendIO via self._printer
                if cached_ams_data is not None and isinstance(cached_ams_data, list) and len(cached_ams_data) > 0: # Check if raw data was non-empty list
                     self._printer.sendIO("echo: Parsing resulted in an empty list of trays (check raw data and logs).\n")
                elif cached_ams_data is None:
                    # Already handled by the initial check, but belt and suspenders
                    pass
                elif isinstance(cached_ams_data, list) and len(cached_ams_data) == 0:
                     self._printer.sendIO("echo: Cached AMS data list is empty.\n")
                else:
                    # Covers cases where cached_ams_data is not None, not a list, but not malformed in the initial check
                     self._printer.sendIO(f"echo: Cached AMS data is not a list ({type(cached_ams_data).__name__}). Cannot parse trays.\n")


            # Corrected to call sendIO via self._printer
            self._printer.sendIO("echo: -----------------------------------------------------\n")

        # Note: The original M1111 handler also sent an "ok N+1".
        # If this report_physical_ams_status method is only called for debugging
        # within _get_print_command_for_file, you probably *don't* want to send
        # an extra "ok" here, as it's not a G-code command handler itself.
        # The _get_print_command_for_file method's result (the print command)
        # will eventually be handled by other parts of the plugin.
        # I have removed the sendIO(f"ok {next_expected_line}\n") from this method.
        # If this method is intended to be called as a G-code handler itself,
        # the return True and sendIO("ok...") logic should be re-added,
        # but the M1111 handler already exists for that.


    def start_new_print(self):
        """
        Initiates a new print job using the currently selected file.
        """
        selected_file = self._printer.selected_file
        if selected_file is None:
            self._log.warn("Cannot start print job if file was not selected")
            return

        self._log.info(f"Attempting to start print for selected file: {selected_file.file_name} (SD path: {getattr(selected_file, 'path', 'N/A')})")
        # Optional: Log more details about the selected_file object if debugging is needed
        # self._log.debug(f"Selected FileInfo object: {selected_file}")
        # self._log.debug(f"Selected FileInfo object attributes: {dir(selected_file)}")
        # if hasattr(selected_file, 'origin'): self._log.debug(f"selected_file.origin: {selected_file.origin}")
        # if hasattr(selected_file, 'dosname'): self._log.debug(f"selected_file.dosname: {selected_file.dosname}")
        # if hasattr(selected_file, 'path'): self._log.debug(f"selected_file.path: {selected_file.path}")


        # Call the method to get the print command payload
        # Pass the selected_file to the method to access its name
        print_command = self._get_print_command_for_file(selected_file)

        # Check if print_command was successfully constructed (it returns None on failure)
        if print_command:
            self._log.debug(f"Sending print command: {json.dumps(print_command, indent=2)}") # Use dumps for readable log
            # Assumes self._printer.bambu_client is available and has a publish method
            if hasattr(self._printer, 'bambu_client') and hasattr(self._printer.bambu_client, 'publish'):
                if self._printer.bambu_client.publish(print_command):
                    self._log.info(f"Sent print command for {selected_file.file_name}. Print should be starting.")
                else:
                    self._log.error(f"Failed to send print command for {selected_file.file_name}.")
            else:
                 self._log.error("Bambu client not available to publish print command.")
                 self._log.warning(f"Failed to start print for {selected_file.file_name} - Client not ready.")

        else:
            self._log.error(f"Failed to construct print command for {selected_file.file_name}. Print aborted.")


    # --- _get_print_command_for_file method (Origin check bypassed) ---
    # Place this method inside your IdleState class
    def _get_print_command_for_file(self, selected_file: FileInfo):
        """
        Constructs the print command payload for a selected file on the SD card.

        Args:
            selected_file (FileInfo): The FileInfo object for the file selected on the SD card.

        Returns:
            dict | None: The print command dictionary payload, or None if construction fails.
        """
        # >>> WARNING: The check for selected_file.origin has been removed as requested.
        # >>> This method will now attempt to process ANY selected file as an SD card file
        # >>> and look for its associated local metadata JSON based on dosname.
        # >>> This may cause errors if the selected file is NOT from the SD card.
        self._log.warning(f"Origin check bypassed for selected file: {selected_file.file_name}. Assuming SD origin.")

        # Check for essential attributes needed for SD print logic
        if not hasattr(selected_file, 'path') or not selected_file.path:
             self._log.error(f"Selected file {selected_file.file_name} lacks a valid 'path' attribute for SD card. Cannot construct print command.")
             return None
        # Note: dosname check for JSON lookup was handled within _read_local_metadata_json (though the function now reads .metadata.json)


        # URL to print. Root path, protocol can vary. E.g., if sd card, "ftp:///myfile.3mf", "ftp:///cache/myotherfile.3mf"
        # selected_file.path should represent the path on the SD card filesystem (e.g., 'filename.3mf' or 'folder/filename.3mf')
        # Assumes self._printer._settings is available
        try:
            device_type = self._printer._settings.get(["device_type"])
            filesystem_root = (
                "file:///mnt/sdcard/" # Path for X1/X1C
                if device_type in ["X1", "X1C"]
                else "file:///sdcard/" # Path for other models like P1P/P1S? Verify this path in docs.
            )
        except Exception as e:
            self._log.error(f"Error getting device type or filesystem root: {e}. Cannot construct print URL.")
            return None

        # selected_file.path.as_posix() gets the path using forward slashes, suitable for URLs.
        # Ensure selected_file.path is treated as a Path object if needed, or just a string that works with os.path/f-strings
        # Assuming selected_file.path is a string or has .as_posix() if it's a custom FileInfo object
        file_url_path = selected_file.path
        if hasattr(file_url_path, 'as_posix'):
             file_url_path = file_url_path.as_posix()
        elif not isinstance(file_url_path, str):
             self._log.warning(f"selected_file.path is not a string and has no .as_posix(): {type(file_url_path)}. Using as is, may fail.")
             file_url_path = str(file_url_path)


        # --- Get MD5 Hash from .metadata.json ---
        # Reads the single .metadata.json file
        full_metadata_dict = self._read_all_metadata_json() # Call the correct function

        md5_hash = "" # Initialize md5_hash

        if full_metadata_dict and isinstance(full_metadata_dict, dict):
            # Get the specific metadata entry for this .3mf file using its full name
            metadata_for_this_file_entry = full_metadata_dict.get(selected_file.file_name)

            if metadata_for_this_file_entry and isinstance(metadata_for_this_file_entry, dict):
                self._log.debug(f"Found metadata entry for {selected_file.file_name} in .metadata.json.")

                # Extract the 'hash' which is the MD5
                # Based on your example, it's directly under the .3mf filename key.
                md5_hash = metadata_for_this_file_entry.get("hash", "") # Look for "hash" key

                if md5_hash and isinstance(md5_hash, str): # Also check if it's a string
                    self._log.debug(f"Found MD5 hash ('hash' key) in .metadata.json: {md5_hash}")
                else:
                    self._log.warning(f"Metadata entry for {selected_file.file_name} found in .metadata.json, but 'hash' key is missing, empty, or not a string.")
                    md5_hash = "" # Ensure it's empty string if not found/valid

            else:
                 self._log.warning(f"No metadata entry found for {selected_file.file_name} in .metadata.json.")

        else:
            self._log.warning("Could not read or parse .metadata.json or it's not a dictionary. Cannot get MD5.")


        # --- Get Sliced Filament Metadata from file_name.3mf.json ---
        # Construct the expected filename for the file-specific metadata JSON WITH the leading period
        file_metadata_json_filename = "." + selected_file.file_name + ".json" # Corrected filename construction

        # Read this file using the new helper function
        sliced_metadata_for_file = self._read_file_metadata_json(file_metadata_json_filename)

        # *** DEBUG LOG: Print the structure of sliced_metadata_for_file (from the file-specific JSON) ***
        # This will show us the exact keys and nesting that generate_ams_mapping will receive
        self._log.debug(f"Structure of file-specific metadata ({file_metadata_json_filename}): {json.dumps(sliced_metadata_for_file, indent=2) if sliced_metadata_for_file else 'None'}")


        # --- Get Physical AMS Data from Main Plugin Instance Cache ---
        # Retrieve the live AMS data from the main plugin instance's cached attribute.
        # This aligns with where the M1111 command and report_physical_ams_status access the data.
        # Assuming the main plugin instance (self._printer) has an ams_data attribute
        # Use getattr for safety in case the attribute doesn't exist or is None
        ams_data = getattr(self._printer, 'ams_data', None)

        # NOTE: Removed the 'if not ams_data:' check and the associated warning here.
        # The report_physical_ams_status call below will indicate if data is missing,
        # and the generate_ams_mapping function is designed to handle None/empty ams_data.


        # *** Call report_physical_ams_status here to examine the ams_data cached on self._printer ***
        # This will output details about the physical AMS status to the terminal/logs for debugging.
        # This requires report_physical_ams_status to be a method of this class, which it is.
        # It accesses the cached data from self._printer.ams_data internally (the same data we just got).
        self._log.debug("Calling report_physical_ams_status to debug physical AMS data cached on self._printer.")
        self.report_physical_ams_status() # Call the method here


        # --- Generate the AMS Mapping List ---
        # Call the function to get the ams_mapping list [..., ...] or [-1, ...] or []
        # Pass the file-specific metadata (containing the filaments list) and the retrieved ams_data.
        # Pass your logger object (self._log) to the function.
        # generate_ams_mapping is now a method of IdleState, so call it with self.
        # Pass sliced_metadata_for_file (the dictionary from the file-specific JSON)
        calculated_ams_mapping_list = self.generate_ams_mapping(sliced_metadata_for_file, ams_data, self._log)

        # Check if the mapping generation returned None unexpectedly (should return a list)
        if calculated_ams_mapping_list is None:
             self._log.error("AMS mapping generation returned None unexpectedly. Cannot start print.")
             return None

        self._log.debug(f"Generated AMS mapping list: {calculated_ams_mapping_list}")


        # --- Construct the Print Command Payload ---
        # Assumes selected_file has file_name, path, md5, etc. attributes
        # Assumes self._printer._settings is available
        # Constructing the dictionary with keys in the requested order
        print_command = {
            "print": {
                "sequence_id": str(int(time.time() * 1000)), # Use a dynamic timestamp as a string ID
                "command": "project_file",
                "param": "Metadata/plate_1.gcode", # Static param based on .3mf structure

                # Order from the template provided
                "project_id": "0", # Placeholder, might be in metadata?
                "profile_id": "0", # Placeholder, might be in metadata?
                "task_id": "0",    # Placeholder, might be in metadata?
                "subtask_id": "0", # Placeholder, might be in metadata?
                "subtask_name": selected_file.file_name, # From FileInfo

                # "file": "", # Not needed as url is used

                "url": f"{filesystem_root}{file_url_path}", # Constructed URL from FileInfo

                "md5": md5_hash, # From .metadata.json

                "timelapse": self._printer._settings.get_boolean(["timelapse"]), # From settings
                # Note: Template uses "bed_levelling", our code uses "bed_leveling". Using consistent spelling.
                "bed_leveling": self._printer._settings.get_boolean(["bed_leveling"]), # From settings
                "flow_cali": self._printer._settings.get_boolean(["flow_cali"]), # From settings
                "vibration_cali": self._printer._settings.get_boolean(["vibration_cali"]), # From settings
                "layer_inspect": self._printer._settings.get_boolean(["layer_inspect"]), # From settings

                # Placing ams_mapping before use_ams as per the template's key order
                # Note: Template had ams_mapping: "", but we need the list [3] for AMS print
                "ams_mapping": calculated_ams_mapping_list, # Calculated mapping [3] or [-1,...]

                # Note: Template had use_ams: false, but we need true for AMS print
                "use_ams": self._printer._settings.get_boolean(["use_ams"]), # From settings (true)

            }
        }
        self._log.debug(f"Constructed print command payload for {selected_file.file_name}.")

        # Return the constructed dictionary payload
        return print_command

    # ... (other methods of the IdleState class go here) ...

# End of IdleState class
