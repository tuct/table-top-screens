#pragma once

#include <cstdio>
#include <memory>
#include <string>

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/display/display.h"
#include "esphome/components/http_request/http_request.h"
#include "esphome/components/sd_spi/sd_spi.h"

#ifdef USE_ESP32

namespace esphome {
namespace sd_clip {

/// Plays a clip of raw RGB565 frames stored on an SD card.
///
/// The point of this component is what it does NOT do. The network path costs
/// a ~1.8 s JPEG decode per 800x480 frame on an ESP32-S3, which caps playback
/// near 0.33 fps; PSRAM caching removes the network but is bounded to roughly
/// 40 frames before memory runs out. Raw frames on a card remove the decode
/// AND the bound: a frame is read straight into a buffer already in the
/// panel's pixel format and handed to draw_pixels_at().
///
/// The cost is size. A frame is always width*height*2 bytes -- 768 KB at
/// 800x480 against ~33 KB as JPEG -- so this trade only pays because the card
/// has gigabytes and the decode was the actual bottleneck.
class SdClip : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  /// After sd_spi (DATA), because caching and playback both need the mount.
  float get_setup_priority() const override { return setup_priority::LATE; }

  void set_sd(sd_spi::SdSpi *sd) { this->sd_ = sd; }
  void set_display(display::Display *disp) { this->display_ = disp; }
  void set_http(http_request::HttpRequestComponent *http) { this->http_ = http; }
  void set_size(int width, int height) {
    this->width_ = width;
    this->height_ = height;
  }
  void set_directory(const std::string &dir) { this->directory_ = dir; }

  /// Bytes in one frame. Fixed, which is why no header is needed on disk.
  size_t frame_bytes() const { return static_cast<size_t>(this->width_) * this->height_ * 2; }

  /// Download ONE frame to the card if it is not already there.
  ///
  /// One frame, not the whole clip, because a clip is tens of megabytes: a
  /// blocking loop over 40 frames would stall ESPHome's main loop for half a
  /// minute, starving WiFi and the web server. The caller ticks through
  /// indices from an interval instead, so the device stays responsive while
  /// caching. `base_url` must already select the raw format; the frame index
  /// is appended as `&n=<i>`.
  ///
  /// Returns true if the frame is on the card afterwards -- including when it
  /// was already there, which is what makes a reboot free rather than a
  /// re-download.
  bool cache_one(const std::string &base_url, int index);

  /// True if frame `index` is already on the card at the expected size.
  bool has_frame(int index);

  /// Count the frames actually on the card and adopt them. Returns the count,
  /// which is also the index of the first missing frame.
  ///
  /// Needed because frames can be present without this component having
  /// written them -- left by a previous run, or by a boot where the card
  /// mounted only after setup() had already given up counting. Without this,
  /// cached() stays 0 and playback never starts even though the clip is right
  /// there on the card.
  int rescan();

  /// Make sure the cached frames belong to `token`, clearing them if not.
  ///
  /// Frames are named by index alone, so a different clip of the same length
  /// would silently replay the old one. The token is the server's content URL,
  /// which carries a version of the current image.
  /// Returns true if the existing cache was kept.
  bool ensure_clip(const std::string &token);

  /// Write an already-decoded RGB565 frame to the card.
  ///
  /// This is the compressed-transport path: something else (online_image)
  /// fetches a JPEG a fraction of the size and decodes it into a buffer, and
  /// we persist the decoded pixels. The decode is paid ONCE per frame at cache
  /// time rather than on every playback, which is the whole point -- and the
  /// network carries ~130 KB instead of 768 KB.
  bool write_frame(int index, const uint8_t *data, size_t len);

  float last_write_ms() const { return this->last_write_us_ / 1000.0f; }
  /// Milliseconds the last cache_one() spent waiting on HTTP, as opposed to
  /// writing. Splitting these is the only way to tell whether the network or
  /// the card is the limit.
  float last_http_ms() const { return this->last_http_us_ / 1000.0f; }

  /// Read frame `index` off the card and blit it, one horizontal band at a
  /// time. Returns false if the frame is missing or short.
  ///
  /// Banded, not whole-frame, for one specific reason. ESP-IDF's
  /// sdmmc_read_sectors() only uses its fast DMA path when the destination is
  /// internal RAM; for a PSRAM destination it falls back to a 512-byte bounce
  /// buffer and does one single-block read plus a memcpy PER SECTOR. A 768 KB
  /// frame is 1500 sectors, which measured at 2335 ms -- 329 KB/s, about 13%
  /// of the bus. Reading into a small internal DMA-capable buffer instead
  /// keeps the fast path, and the band is blitted straight to its row range,
  /// so no full-frame buffer is needed at all.
  bool show(int index);

  int cached() const { return this->cached_; }
  /// Milliseconds spent in the last card read and the last blit. Exposed so
  /// the real frame budget is visible without a serial cable.
  float last_read_ms() const { return this->last_read_us_ / 1000.0f; }
  float last_blit_ms() const { return this->last_blit_us_ / 1000.0f; }

  // -- stills ---------------------------------------------------------------
  //
  // A still is a one-frame clip with its own file, and exists for boards that
  // cannot afford a decode buffer: the raw frame streams from HTTP straight to
  // the card, and is shown with the same banded read-and-blit as a clip. So no
  // step ever holds more than one band in RAM -- no PSRAM needed for content.

  /// What the last still fetch did. Read once with take_still_event().
  enum StillEvent { STILL_NONE = 0, STILL_UPDATED, STILL_UNCHANGED, STILL_FAILED };

  /// True if a complete still is on the card.
  bool has_still();

  /// Begin fetching a still. `url` must select raw RGB565 at the panel size.
  ///
  /// Sends If-None-Match with the ETag of the still already on the card, so an
  /// unchanged picture costs one 304. The body is streamed to the card from
  /// loop() a bounded amount per tick, so the device stays responsive during a
  /// 768 KB transfer. Returns false if a fetch could not be started; the result
  /// of one that was is reported through take_still_event().
  bool start_still(const std::string &url);
  bool still_busy() const { return this->still_http_ != nullptr; }
  /// Returns and clears the last StillEvent.
  int take_still_event() {
    const int e = this->still_event_;
    this->still_event_ = STILL_NONE;
    return e;
  }

  /// Blit the still on the card. Returns false if there is none.
  bool show_still();

 protected:
  bool ensure_buffer_();
  /// Banded read-and-blit of one raw frame file. Shared by clips and stills.
  bool blit_file_(const std::string &full, const char *what);
  void finish_still_(bool ok);
  std::string still_path_(const char *name) const;
  std::string frame_path_(int index) const;
  /// Absolute path of frame `index`, including the card's mount point.
  std::string full_path_(int index) const;

  sd_spi::SdSpi *sd_{nullptr};
  display::Display *display_{nullptr};
  http_request::HttpRequestComponent *http_{nullptr};
  int width_{0};
  int height_{0};
  std::string directory_{"/clip"};

  /// One band of rows, in INTERNAL DMA-capable RAM -- see show().
  uint8_t *buffer_{nullptr};
  /// Rows per band. Chosen so a band is a whole number of 512-byte sectors,
  /// because a partial sector would drag FATFS back through its own window.
  int band_rows_{0};
  int cached_{0};
  uint32_t last_read_us_{0};
  uint32_t last_blit_us_{0};
  uint32_t last_write_us_{0};
  uint32_t last_http_us_{0};

  std::shared_ptr<http_request::HttpContainer> still_http_;
  std::unique_ptr<uint8_t[]> still_chunk_;
  FILE *still_file_{nullptr};
  std::string still_etag_;
  size_t still_written_{0};
  uint32_t still_last_data_{0};
  uint32_t still_started_{0};
  int still_event_{STILL_NONE};
};

}  // namespace sd_clip
}  // namespace esphome

#endif  // USE_ESP32
