# coding=utf-8

__plugin_name__ = "Bambu Printer"
__plugin_pythoncompat__ = ">=3.7,<4"

from .bambu_print_plugin import BambuPrintPlugin


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
        "octoprint.server.api.before_request": __plugin_implementation__._hook_octoprint_server_api_before_request,
        "octoprint.server.http.routes": __plugin_implementation__.route_hook,
    }
