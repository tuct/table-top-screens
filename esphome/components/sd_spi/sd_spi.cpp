#include "sd_spi.h"

#ifdef USE_ESP32

#include <cstdio>
#include <cstring>
#include <dirent.h>

#include "esp_heap_caps.h"

#include "esphome/core/log.h"

namespace esphome {
namespace sd_spi {

static const char *const TAG = "sd_spi";

bool SdSpi::try_mount_() {
  // Let the IDF's own SD layers talk. sdmmc_common logs the card's CID/CSD as
  // it initialises, and vfs_fat_sdmmc logs why f_mount refused -- neither of
  // which we can print ourselves on failure, because esp_vfs_fat_sdspi_mount
  // frees the card and nulls our pointer before returning.
  esp_log_level_set("sdmmc_common", ESP_LOG_DEBUG);
  esp_log_level_set("sdmmc_sd", ESP_LOG_DEBUG);
  esp_log_level_set("sdspi_host", ESP_LOG_DEBUG);
  esp_log_level_set("vfs_fat_sdmmc", ESP_LOG_DEBUG);

  sdmmc_host_t host = SDSPI_HOST_DEFAULT();
  host.slot = static_cast<spi_host_device_t>(this->spi_host_);
  host.max_freq_khz = this->max_freq_khz_;

  sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
  slot.host_id = static_cast<spi_host_device_t>(this->spi_host_);
  // No CS pin means the board asserts it some other way -- e.g. the Waveshare
  // 4.3, whose chip select sits on an I2C expander and is simply held low.
  // That is only safe because the card is the sole device on its bus.
  slot.gpio_cs = this->cs_pin_ < 0 ? static_cast<gpio_num_t>(SDSPI_SLOT_NO_CS)
                                   : static_cast<gpio_num_t>(this->cs_pin_);

  esp_vfs_fat_sdmmc_mount_config_t mount_config = {};
  mount_config.format_if_mount_failed = this->format_if_mount_failed_;
  mount_config.max_files = this->max_files_;
  mount_config.allocation_unit_size = 16 * 1024;

  esp_err_t err = esp_vfs_fat_sdspi_mount(this->mount_point_.c_str(), &host, &slot, &mount_config,
                                          &this->card_);
  if (err != ESP_OK) {
    // Deliberately not marking the component FAILED: a missing or unreadable
    // card should not take the whole device down, and the rest of the screen
    // works fine without it.
    this->mounted_ = false;
    this->status_set_error(LOG_STR("SD card not mounted"));
    if (this->retries_ > 0) {
      // Already explained in full on the first attempt; one line is enough.
      ESP_LOGW(TAG, "Still no card: %s", esp_err_to_name(err));
      return false;
    }
    ESP_LOGE(TAG, "Mount failed: %s (%d)", esp_err_to_name(err), err);
    if (err == ESP_FAIL) {
      // ESP_FAIL comes from mount_to_vfs, which runs only AFTER the card has
      // initialised -- so the SPI link is good and it is the filesystem that
      // was refused. Card details below confirm the link and give the size,
      // which is usually the clue: cards over 32 GB ship formatted exFAT,
      // which ESP-IDF's FATFS will not mount.
      ESP_LOGE(TAG, "  Card answered, but its filesystem could not be mounted.");
      ESP_LOGE(TAG, "  Format it FAT32 with an MBR partition table.");
      this->probe_card_(host, slot);
    } else {
      ESP_LOGE(TAG, "  Card did not respond. Check wiring, and that a card is inserted.");
    }
    return false;
  }

  this->mounted_ = true;
  this->status_clear_error();
  ESP_LOGI(TAG, "Mounted %s", this->mount_point_.c_str());
  this->self_test_();
  return true;
}

void SdSpi::setup() {
  if (this->try_mount_())
    return;

  // Keep trying. A card can be seated late, or fail to initialise once on a
  // cold boot, and with no Home Assistant and no API there is no button to
  // press -- so requiring a reboot to retry would mean requiring a power
  // cycle. Retries are quiet after the first: the detailed probe already ran.
  this->set_interval("remount", RETRY_INTERVAL_MS, [this]() {
    if (this->mounted_) {
      this->cancel_interval("remount");
      return;
    }
    this->retries_++;
    ESP_LOGD(TAG, "Retrying mount (attempt %u)", static_cast<unsigned>(this->retries_));
    if (this->try_mount_()) {
      ESP_LOGI(TAG, "Card mounted on retry %u", static_cast<unsigned>(this->retries_));
      this->cancel_interval("remount");
    }
  });
}

void SdSpi::self_test_() {
  // Runs on every successful mount, because there is no Home Assistant here
  // and no way to press a button: a card that mounts but cannot be read or
  // written has to announce itself in the log, unprompted.
  uint64_t total = 0, free_bytes = 0;
  if (esp_vfs_fat_info(this->mount_point_.c_str(), &total, &free_bytes) == ESP_OK) {
    ESP_LOGI(TAG, "  %llu MB free of %llu MB", free_bytes / (1024ULL * 1024ULL),
             total / (1024ULL * 1024ULL));
  }

  const std::vector<std::string> entries = this->list_directory("/", 16);
  ESP_LOGI(TAG, "  Root holds %u entries:", static_cast<unsigned>(entries.size()));
  for (const auto &e : entries) {
    ESP_LOGI(TAG, "    %s", e.c_str());
  }

  // Write-read-delete round trip. Mounting only proves FATFS parsed the
  // filesystem; it does not prove the card accepts writes, which is what the
  // frame cache will depend on.
  const std::string probe = this->resolve_("/.esphome_selftest");
  static const char PAYLOAD[] = "tabletop";
  const size_t len = sizeof(PAYLOAD) - 1;

  FILE *f = fopen(probe.c_str(), "wb");
  if (f == nullptr) {
    ESP_LOGE(TAG, "  Self-test: cannot create a file -- card is read-only or full.");
    this->status_set_error(LOG_STR("SD card not writable"));
    return;
  }
  const size_t wrote = fwrite(PAYLOAD, 1, len, f);
  fclose(f);

  char back[sizeof(PAYLOAD)] = {};
  const size_t read = this->read_into("/.esphome_selftest", reinterpret_cast<uint8_t *>(back), len);
  ::remove(probe.c_str());

  if (wrote == len && read == len && memcmp(back, PAYLOAD, len) == 0) {
    ESP_LOGI(TAG, "  Self-test: write and read back OK.");
  } else {
    ESP_LOGE(TAG, "  Self-test: FAILED (wrote %u, read %u of %u) -- data is not surviving.",
             static_cast<unsigned>(wrote), static_cast<unsigned>(read),
             static_cast<unsigned>(len));
    this->status_set_error(LOG_STR("SD card self-test failed"));
  }
}

void SdSpi::dump_config() {
  ESP_LOGCONFIG(TAG, "SD card (SPI):");
  ESP_LOGCONFIG(TAG, "  Mount point: %s", this->mount_point_.c_str());
  ESP_LOGCONFIG(TAG, "  SPI host: %d", this->spi_host_);
  if (this->cs_pin_ < 0) {
    ESP_LOGCONFIG(TAG, "  CS pin: none (held externally)");
  } else {
    ESP_LOGCONFIG(TAG, "  CS pin: GPIO%d", this->cs_pin_);
  }
  ESP_LOGCONFIG(TAG, "  Max frequency: %d kHz", this->max_freq_khz_);
  ESP_LOGCONFIG(TAG, "  Mounted: %s", YESNO(this->mounted_));
  if (this->mounted_ && this->card_ != nullptr) {
    ESP_LOGCONFIG(TAG, "  Card: %s, %llu MB", this->card_->cid.name,
                  (static_cast<uint64_t>(this->card_->csd.capacity) * this->card_->csd.sector_size) /
                      (1024ULL * 1024ULL));
  }
}

void SdSpi::probe_card_(sdmmc_host_t host, sdspi_device_config_t slot) {
  // The mount helper has already torn its own host down by the time it returns
  // an error, so we start from scratch. Everything here is undone before we
  // leave, so a later remount attempt is unaffected.
  esp_err_t err = sdspi_host_init();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "  Probe: host init failed: %s", esp_err_to_name(err));
    return;
  }

  sdspi_dev_handle_t handle;
  err = sdspi_host_init_device(&slot, &handle);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "  Probe: attaching to the bus failed: %s", esp_err_to_name(err));
    sdspi_host_deinit();
    return;
  }
  host.slot = handle;

  sdmmc_card_t card = {};
  err = sdmmc_card_init(&host, &card);
  if (err == ESP_OK) {
    const uint64_t bytes = static_cast<uint64_t>(card.csd.capacity) * card.csd.sector_size;
    ESP_LOGE(TAG, "  Probe: card OK -- so the wiring is fine and only the filesystem is wrong.");
    ESP_LOGE(TAG, "    Name: %s", card.cid.name);
    ESP_LOGE(TAG, "    Capacity: %llu MB (%llu GB)", bytes / (1024ULL * 1024ULL),
             bytes / (1000ULL * 1000ULL * 1000ULL));
    ESP_LOGE(TAG, "    Sector size: %d bytes, speed %d kHz", card.csd.sector_size,
             card.max_freq_khz);
    if (bytes > 32ULL * 1024 * 1024 * 1024) {
      ESP_LOGE(TAG, "    Over 32 GB: this is almost certainly exFAT, which cannot be mounted.");
    }
    sdmmc_card_print_info(stdout, &card);
    this->probe_sector0_(&card);
  } else {
    ESP_LOGE(TAG, "  Probe: card did NOT initialise: %s -- so this is wiring or CS, not the",
             esp_err_to_name(err));
    ESP_LOGE(TAG, "    filesystem, and the message above is misleading.");
  }

  sdspi_host_remove_device(handle);
  sdspi_host_deinit();
}

void SdSpi::probe_sector0_(sdmmc_card_t *card) {
  // Read the very first sector and decode it ourselves. FATFS only tells us
  // "failed to mount" without saying what it found; sector 0 says plainly
  // whether this is an MBR, and what filesystem each partition claims to be.
  uint8_t *buf = static_cast<uint8_t *>(heap_caps_malloc(512, MALLOC_CAP_DMA));
  if (buf == nullptr) {
    ESP_LOGE(TAG, "    (no DMA memory to read sector 0)");
    return;
  }

  const esp_err_t err = sdmmc_read_sectors(card, buf, 0, 1);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "    Sector 0 unreadable: %s", esp_err_to_name(err));
    heap_caps_free(buf);
    return;
  }

  if (buf[510] != 0x55 || buf[511] != 0xAA) {
    ESP_LOGE(TAG, "    Sector 0 has no 0x55AA signature -- the card is unpartitioned or wiped.");
    heap_caps_free(buf);
    return;
  }

  // An exFAT or FAT volume placed directly at sector 0 (no partition table)
  // starts with a jump instruction and names itself a few bytes in.
  if (buf[0] == 0xEB || buf[0] == 0xE9) {
    char name[9] = {};
    memcpy(name, buf + 3, 8);
    ESP_LOGE(TAG, "    Sector 0 is a boot sector, not an MBR. OEM name: '%s'", name);
    if (memcmp(buf + 3, "EXFAT", 5) == 0) {
      ESP_LOGE(TAG, "    -> exFAT. ESP-IDF's FATFS is not built with exFAT support.");
    }
    ESP_LOGE(TAG, "    -> Superfloppy layout (no partition table); IDF wants an MBR.");
    heap_caps_free(buf);
    return;
  }

  ESP_LOGE(TAG, "    Sector 0 is an MBR. Partitions:");
  bool any = false;
  for (int i = 0; i < 4; i++) {
    const uint8_t *e = buf + 446 + i * 16;
    const uint8_t type = e[4];
    if (type == 0x00)
      continue;
    any = true;
    const uint32_t start = e[8] | (e[9] << 8) | (e[10] << 16) | (static_cast<uint32_t>(e[11]) << 24);
    const uint32_t count = e[12] | (e[13] << 8) | (e[14] << 16) | (static_cast<uint32_t>(e[15]) << 24);
    const char *label;
    switch (type) {
      case 0x01: label = "FAT12 -- OK"; break;
      case 0x04: case 0x06: case 0x0E: label = "FAT16 -- OK"; break;
      case 0x0B: case 0x0C: label = "FAT32 -- OK"; break;
      case 0x07: label = "exFAT or NTFS -- NOT mountable"; break;
      case 0x83: label = "Linux -- NOT mountable"; break;
      case 0xEE: label = "GPT protective -- card is GPT, IDF needs MBR"; break;
      default: label = "unknown -- NOT mountable"; break;
    }
    ESP_LOGE(TAG, "      %d: type 0x%02X (%s), start %u, %u MB", i + 1, type, label, start,
             static_cast<unsigned>(static_cast<uint64_t>(count) * 512 / (1024 * 1024)));
  }
  if (!any) {
    ESP_LOGE(TAG, "      (none -- the partition table is empty)");
  }
  heap_caps_free(buf);
}

std::string SdSpi::resolve_(const std::string &path) const {
  if (path.rfind(this->mount_point_, 0) == 0)
    return path;
  if (!path.empty() && path[0] == '/')
    return this->mount_point_ + path;
  return this->mount_point_ + "/" + path;
}

size_t SdSpi::file_size(const std::string &path) {
  if (!this->mounted_)
    return 0;
  const std::string full = this->resolve_(path);
  FILE *f = fopen(full.c_str(), "rb");
  if (f == nullptr)
    return 0;
  fseek(f, 0, SEEK_END);
  const long size = ftell(f);
  fclose(f);
  return size < 0 ? 0 : static_cast<size_t>(size);
}

size_t SdSpi::read_into(const std::string &path, uint8_t *dst, size_t max_len) {
  if (!this->mounted_ || dst == nullptr || max_len == 0)
    return 0;
  const std::string full = this->resolve_(path);
  FILE *f = fopen(full.c_str(), "rb");
  if (f == nullptr) {
    ESP_LOGW(TAG, "Cannot open %s", full.c_str());
    return 0;
  }
  const size_t read = fread(dst, 1, max_len, f);
  fclose(f);
  return read;
}

std::vector<uint8_t> SdSpi::read_file(const std::string &path) {
  std::vector<uint8_t> out;
  const size_t size = this->file_size(path);
  if (size == 0)
    return out;
  out.resize(size);
  const size_t read = this->read_into(path, out.data(), size);
  out.resize(read);
  return out;
}

std::vector<std::string> SdSpi::list_directory(const std::string &path, size_t max_entries) {
  std::vector<std::string> out;
  if (!this->mounted_)
    return out;
  const std::string full = this->resolve_(path);
  DIR *dir = opendir(full.c_str());
  if (dir == nullptr) {
    ESP_LOGW(TAG, "Cannot open directory %s", full.c_str());
    return out;
  }
  struct dirent *entry;
  while (out.size() < max_entries && (entry = readdir(dir)) != nullptr) {
    out.emplace_back(entry->d_name);
  }
  closedir(dir);
  return out;
}

}  // namespace sd_spi
}  // namespace esphome

#endif  // USE_ESP32
