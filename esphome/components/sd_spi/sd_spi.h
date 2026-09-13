#pragma once

#include <string>
#include <vector>

#include "esphome/core/component.h"

#ifdef USE_ESP32
#include "driver/sdspi_host.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"

// Older IDF headers spell the "no chip select" sentinel differently.
#ifndef SDSPI_SLOT_NO_CS
#define SDSPI_SLOT_NO_CS GPIO_NUM_NC
#endif

namespace esphome {
namespace sd_spi {

/// An SD card attached as a device on an SPI bus ESPHome already initialised.
///
/// esp_vfs_fat_sdspi_mount() deliberately does not initialise the bus -- it
/// adds a device to an existing one -- which is exactly what we need, because
/// ESPHome's `spi:` component has already called spi_bus_initialize().
class SdSpi : public Component {
 public:
  void setup() override;
  /// How often to retry a failed mount. Slow enough not to spam a board with
  /// no card in it, fast enough that inserting one is picked up promptly.
  static const uint32_t RETRY_INTERVAL_MS = 10000;
  void dump_config() override;
  /// After the SPI bus (BUS) and after displays (HARDWARE), because a shared
  /// bus must be up first, and Seeed document that the round display has to be
  /// initialised before its card.
  float get_setup_priority() const override { return setup_priority::DATA; }

  void set_cs_pin(int pin) { this->cs_pin_ = pin; }
  void set_spi_host(int host) { this->spi_host_ = host; }
  void set_mount_point(const std::string &mount_point) { this->mount_point_ = mount_point; }
  void set_format_if_mount_failed(bool v) { this->format_if_mount_failed_ = v; }
  void set_max_files(int v) { this->max_files_ = v; }
  void set_max_freq_khz(int v) { this->max_freq_khz_ = v; }

  bool is_mounted() const { return this->mounted_; }
  /// Where the card is mounted, so callers can build absolute paths without
  /// assuming "/sd".
  const std::string &mount_point() const { return this->mount_point_; }

  /// Size in bytes, or 0 if missing/unmounted.
  size_t file_size(const std::string &path);

  /// Read up to `max_len` bytes into a caller-owned buffer; returns bytes read.
  ///
  /// This is the one that matters for video: a frame can be read straight into
  /// a buffer that is then blitted, with no allocation per frame. Returning a
  /// std::vector instead would mean allocating and freeing hundreds of
  /// kilobytes for every frame.
  size_t read_into(const std::string &path, uint8_t *dst, size_t max_len);

  /// Convenience for small files. Allocates; do not use per video frame.
  std::vector<uint8_t> read_file(const std::string &path);

  /// Filenames (not full paths) directly inside `path`.
  std::vector<std::string> list_directory(const std::string &path, size_t max_entries = 64);

 protected:
  /// Accepts "/clip/0.565" or "clip/0.565"; both resolve under the mount point.
  std::string resolve_(const std::string &path) const;

  /// Re-initialise the card by hand after a failed mount, purely to report it.
  ///
  /// esp_vfs_fat_sdspi_mount() frees the card struct and nulls our pointer on
  /// every failure path, so after a failure we know nothing about the card --
  /// not even whether it responded. Doing the init ourselves into a struct we
  /// own answers that, and gives the capacity, which is the usual clue: cards
  /// over 32 GB ship exFAT, which ESP-IDF's FATFS cannot mount.
  void probe_card_(sdmmc_host_t host, sdspi_device_config_t slot);

  /// Decode sector 0 (MBR or boot sector) to say exactly why FATFS refused.
  void probe_sector0_(sdmmc_card_t *card);

  /// Report free space, list the root, and round-trip a small file.
  void self_test_();

  /// One mount attempt. Returns true if the card is mounted afterwards.
  bool try_mount_();

  uint32_t retries_{0};

  int cs_pin_{-1};
  int spi_host_{1};  // SPI2_HOST
  std::string mount_point_{"/sd"};
  bool format_if_mount_failed_{false};
  int max_files_{4};
  int max_freq_khz_{20000};

  bool mounted_{false};
  sdmmc_card_t *card_{nullptr};
};

}  // namespace sd_spi
}  // namespace esphome
#endif  // USE_ESP32
