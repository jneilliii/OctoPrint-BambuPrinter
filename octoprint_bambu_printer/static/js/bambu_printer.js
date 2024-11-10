/*
 * View model for OctoPrint-BambuPrinter
 *
 * Author: jneilliii
 * License: AGPLv3
 */

$(function () {
    function Bambu_printerViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];
        self.filesViewModel = parameters[1];
        self.loginStateViewModel = parameters[2];
        self.accessViewModel = parameters[3];
        self.timelapseViewModel = parameters[4];

        self.use_ams = true;
        self.ams_mapping = ko.observableArray([]);

        self.job_info = ko.observable();

        self.auth_type = ko.observable("");

        self.show_password = ko.pureComputed(function(){
            return self.settingsViewModel.settings.plugins.bambu_printer.auth_token() === '';
        });

        self.show_verification = ko.pureComputed(function(){
            return self.auth_type() !== '';
        });

        self.ams_mapping_computed = function(){
            var output_list = [];
            var index = 0;

            ko.utils.arrayForEach(self.settingsViewModel.settings.plugins.bambu_printer.ams_data(), function(item){
                if(item){
                    output_list = output_list.concat(item.tray());
                }
            });

            ko.utils.arrayForEach(output_list, function(item){
                item["index"] = ko.observable(index);
                index++;
            });

            return output_list;
        };

        self.getAuthToken = function (data) {
            self.settingsViewModel.settings.plugins.bambu_printer.auth_token("");
            self.auth_type("");
            OctoPrint.simpleApiCommand("bambu_printer", "register", {
                "email": self.settingsViewModel.settings.plugins.bambu_printer.email(),
                "password": $("#bambu_cloud_password").val(),
                "region": self.settingsViewModel.settings.plugins.bambu_printer.region(),
                "auth_token": self.settingsViewModel.settings.plugins.bambu_printer.auth_token()
            })
                .done(function (response) {
                    self.auth_type(response.auth_response);
                });
        };

        self.verifyCode = function (data) {
            self.settingsViewModel.settings.plugins.bambu_printer.auth_token("");
            OctoPrint.simpleApiCommand("bambu_printer", "verify", {
                "password": $("#bambu_cloud_verify_code").val(),
                "auth_type": self.auth_type(),
            })
                .done(function (response) {
                    console.log(response);
                    if (response.auth_token) {
                        self.settingsViewModel.settings.plugins.bambu_printer.auth_token(response.auth_token);
                        self.settingsViewModel.settings.plugins.bambu_printer.username(response.username);
                        self.auth_type("");
                    } else if (response.error) {
                        self.settingsViewModel.settings.plugins.bambu_printer.auth_token("");
                        $("#bambu_cloud_verify_code").val("");
                    }
                });
        };

                // initialize list helper
        self.listHelper = new ItemListHelper(
            "timelapseFiles",
            {
                name: function (a, b) {
                    // sorts ascending
                    if (a["name"].toLocaleLowerCase() < b["name"].toLocaleLowerCase())
                        return -1;
                    if (a["name"].toLocaleLowerCase() > b["name"].toLocaleLowerCase())
                        return 1;
                    return 0;
                },
                date: function (a, b) {
                    // sorts descending
                    if (a["date"] > b["date"]) return -1;
                    if (a["date"] < b["date"]) return 1;
                    return 0;
                },
                size: function (a, b) {
                    // sorts descending
                    if (a["bytes"] > b["bytes"]) return -1;
                    if (a["bytes"] < b["bytes"]) return 1;
                    return 0;
                }
            },
            {},
            "name",
            [],
            [],
            CONFIG_TIMELAPSEFILESPERPAGE
        );

        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin != "bambu_printer") {
                return;
            }

            if (data.files !== undefined) {
                self.listHelper.updateItems(data.files);
                self.listHelper.resetPage();
            }

            if (data.job_info !== undefined) {
                self.job_info(data.job_info);
            }
        };

        self.onBeforeBinding = function () {
            $('#bambu_timelapse').appendTo("#timelapse");
        };

        self.onAfterBinding = function () {
            console.log(self.ams_mapping_computed());
        };

        self.showTimelapseThumbnail = function(data) {
            $("#bambu_printer_timelapse_thumbnail").attr("src", data.thumbnail);
            $("#bambu_printer_timelapse_preview").modal('show');
        };

        self.onBeforePrintStart = function(start_print_command, data) {
            self.ams_mapping(self.ams_mapping_computed());
            self.start_print_command = start_print_command;
            self.use_ams = self.settingsViewModel.settings.plugins.bambu_printer.use_ams();
            // prevent starting locally stored files, once data is added to core OctoPrint this
            // could be adjusted to include additional processing like get sliced file's
            // spool assignments and colors from plate_#.json inside 3mf file.
            if(data && data.origin !== "sdcard") {
                return false;
            }
            $("#bambu_printer_print_options").modal('show');
            return false;
        };

        self.toggle_spool_active = function(data) {
            if(data.index() >= 0){
                data.original_index = ko.observable(data.index());
                data.index(-1);
            } else {
                data.index(data.original_index());
            }
        };

        self.cancel_print_options = function() {
            self.settingsViewModel.settings.plugins.bambu_printer.use_ams(self.use_ams);
            $("#bambu_printer_print_options").modal('hide');
        };

        self.accept_print_options = function() {
            console.log("starting print!!!!");
            console.log(self.ams_mapping());
            $("#bambu_printer_print_options").modal('hide');
            var flattened_ams_mapping = ko.utils.arrayMap(self.ams_mapping(), function(item) {
                return item.index();
            });
            self.settingsViewModel.settings.plugins.bambu_printer.ams_mapping(flattened_ams_mapping);
            self.settingsViewModel.saveData(undefined, self.start_print_command);
            // self.settingsViewModel.saveData();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: Bambu_printerViewModel,
        dependencies: ["settingsViewModel", "filesViewModel", "loginStateViewModel", "accessViewModel", "timelapseViewModel"],
        elements: ["#bambu_printer_print_options", "#settings_plugin_bambu_printer", "#bambu_timelapse", "#sidebar_plugin_bambu_printer"]
    });
});
