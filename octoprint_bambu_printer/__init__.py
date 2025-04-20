# coding=utf-8

__plugin_name__ = "Bambu Printer"
__plugin_pythoncompat__ = ">=3.7,<4"

import logging
import octoprint.plugin
from .bambu_print_plugin import PrintWatcher  # Assuming PrintWatcher is defined in bambu_print_plugin.py

class BambuPrintPlugin(octoprint.plugin.SettingsPlugin,
                       octoprint.plugin.StartupPlugin,
                       octoprint.plugin.TemplatePlugin,
                       octoprint.plugin.SimpleApiPlugin):
    """
    BambuPrintPlugin class that integrates with OctoPrint.
    It handles settings, startup, templates, and API endpoints for the plugin.
    """
    
    def __init__(self):
        # Set up logging for the plugin
        self._logger = logging.getLogger("octoprint.plugins.bambu_printer")
        self.watcher = PrintWatcher()  # Initialize the watcher

    def on_after_startup(self):
        """
        This method is called after OctoPrint has started.
        You can use this to perform any actions once OctoPrint is fully up.
        """
        self._logger.info("BambuPrintPlugin started successfully.")
        # Initialize or start any necessary tasks for the plugin
        self.watcher.start()  # Assuming your watcher needs to start on startup

    def get_api_commands(self):
        """
        Defines custom API commands.
        Returns a dictionary of available API commands.
        """
        return {
            "start_watch": ["start_watch", "start"],
            "stop_watch": ["stop_watch", "stop"],
        }

    def on_api_command(self, command, args):
        """
        Handles custom API commands.
        """
        if command == "start_watch":
            self.watcher.start()
            return {"status": "watcher started"}
        elif command == "stop_watch":
            self.watcher.stop()
            return {"status": "watcher stopped"}
        else:
            return {"error": "Unknown command"}

def __plugin_load__():
    """
    Called when the plugin is loaded into OctoPrint.
    Registers hooks and plugin initialization.
    """
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
        "octoprint.server.api.before_request": __plugin_implementation__._hook_octoprint_server_api_before_request,
        "octoprint.server.http.routes": __plugin_implementation__.route_hook,
    }

    # Log successful plugin load
    octoprint.util.get_logger(__plugin_name__).info(f"{__plugin_name__} plugin loaded successfully.")

    try:
        # Additional initialization steps can go here
        pass
    except Exception as e:
        # Log any errors during initialization
        octoprint.util.get_logger(__plugin_name__).error(f"Error during plugin initialization: {e}")
        raise

    # Optionally, you can expose plugin version info and additional metadata
    global __plugin_version__
    __plugin_version__ = "1.0.0"  # Update this to match your actual version
    
    # Log the plugin version for tracking
    octoprint.util.get_logger(__plugin_name__).info(f"{__plugin_name__} version {__plugin_version__} loaded.")
