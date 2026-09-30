// ===========================================================================
//  check_config.cpp - developer tool
//
//  Validates the configuration without opening a camera and prints the result
//  in a form that scripts can assert on:
//
//      ./sidb_check_config                       exit 0 when the config is sane
//      ./sidb_check_config --quiet               only the exit code
//      ./sidb_check_config --require=camera      also require a camera to exist
//
//  scripts/setup.sh and the integration tests use this, so a configuration
//  mistake is caught before anything tries to open a device.
// ===========================================================================
#include <iostream>
#include <string>

#include "camera/CameraManager.h"
#include "utils/Config.h"
#include "utils/Logger.hpp"

int main(int argc, char** argv) {
    std::string config_path;
    bool quiet = false;
    bool require_camera = false;

    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        if (argument == "--config" && i + 1 < argc) {
            config_path = argv[++i];
        } else if (argument.rfind("--config=", 0) == 0) {
            config_path = argument.substr(9);
        } else if (argument == "--quiet") {
            quiet = true;
        } else if (argument == "--require=camera" || argument == "--require-camera") {
            require_camera = true;
        } else if (argument == "-h" || argument == "--help") {
            std::cout << "usage: sidb_check_config [--config=PATH] [--quiet] [--require=camera]\n";
            return 0;
        }
    }

    sidb::log::Logger::instance().set_level(sidb::log::Level::Warning);
    const sidb::Config config = sidb::Config::load(config_path);

    if (!quiet) std::cout << config.describe();

    int status = 0;
    if (!config.warnings.empty()) {
        // Warnings are corrections, not failures - report them and continue.
        if (!quiet) std::cout << "\nconfiguration warnings: " << config.warnings.size() << "\n";
    }
    if (config.api.host != "127.0.0.1" && config.api.host != "localhost") {
        if (!quiet) {
            std::cerr << "warning: the API is configured to bind " << config.api.host
                      << " - this project expects 127.0.0.1\n";
        }
        status = 2;
    }
    if (require_camera) {
        const std::vector<std::string> devices = sidb::CameraManager::available_devices();
        if (devices.empty()) {
            std::cerr << "error: no /dev/video* device found\n";
            return 1;
        }
        if (!quiet) {
            std::cout << "camera devices: " << devices.size() << "\n";
            for (std::size_t i = 0; i < devices.size(); ++i) {
                std::cout << "  [" << i << "] " << devices[i] << "\n";
            }
        }
    }
    return status;
}
