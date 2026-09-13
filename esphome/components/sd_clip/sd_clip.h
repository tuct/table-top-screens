#pragma once

#include <cstdio>
#include <memory>
#include <string>
#include <vector>

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/display/display.h"
#include "esphome/components/http_request/http_request.h"
#include "esphome/components/sd_spi/sd_spi.h"

#ifdef USE_ESP32

// JPEGDEC's header defines the class; forward declarations keep it out of
// every translation unit that includes this one.
class JPEGDEC;
struct jpeg_draw_tag;

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
  /// True while ANY download is running. Stills and clips share one download
  /// slot, so a still cannot start while a clip is downloading either.
  bool still_busy() const { return this->fetch_.http != nullptr; }
  /// Returns and clears the last StillEvent.
  int take_still_event() {
    const int e = this->still_event_;
    this->still_event_ = STILL_NONE;
    return e;
  }

  /// Blit the still on the card. Returns false if there is none.
  bool show_still();

  // -- MJPEG content: stills and clips ---------------------------------------
  //
  // Compressed frames, shown from MEMORY and decoded straight to the panel.
  //
  // The raw paths above avoid decoding because ESPHome's image decoder costs
  // ~1.8 s per 800x480 frame. That cost is not JPEGDEC: runtime_image asks it
  // for RGB8888 and then pushes every pixel through a virtual draw_pixel()
  // with float scaling. Asking for RGB565 and handing each decoded block to
  // draw_pixels_at() -- the approach of github.com/derdacavga/video-Player --
  // is fast enough to play from, so content can stay compressed: ~3-8 KB a
  // frame at 240x240 instead of 115 KB raw.
  //
  // Everything is an ITEM: a TTMJ file whose frames are JPEGs. A still is an
  // item with one frame, a clip one with many. Items are named by a key -- the
  // server's content token, which covers the picture AND its framing -- so an
  // item with a key we already hold is never downloaded again:
  //
  //   memory   PSRAM, least recently used evicted past cache_bytes
  //   card     /mjpeg/cache/<key>.mjp, kept for good (when a card is mounted)
  //   network  only for a key in neither
  //
  // Files copied onto the card by hand sit in /mjpeg and are items too. Two
  // formats are accepted:
  //   TTMJ  "TTMJ" u16 version, u16 w, u16 h, u16 fps, u32 count, then count x
  //         (u32 len, JPEG). Little-endian. What the server sends.
  //   raw   JPEGs back to back, as ffmpeg -c:v mjpeg writes them, optionally
  //         after video-Player's one-byte fps prefix.

  /// What the last request did. Read once with take_item_event().
  enum ItemEvent {
    ITEM_NONE = 0,
    ITEM_SHOWN,    ///< the requested item is on the panel
    ITEM_FAILED,   ///< it could not be downloaded or read; the panel is unchanged
  };

  /// Show content `key`: from memory if it is there, else from the card, else
  /// downloaded from `url` (onto the card when one is mounted, into memory
  /// otherwise). Content already held never touches the network.
  ///
  /// Returns true if the item is shown or on its way (the outcome arrives as
  /// an ItemEvent), false if nothing could start yet -- the download slot is
  /// busy, or the item is not local and `url` is empty -- so ask again later.
  bool request_item(const std::string &key, const std::string &url);
  /// Show /mjpeg/<name>, a file copied onto the card by hand.
  bool request_file(const std::string &name);
  /// True if `key` can be shown without the network.
  bool has_item(const std::string &key);
  int take_item_event() {
    const int e = this->item_event_;
    this->item_event_ = ITEM_NONE;
    return e;
  }

  /// Clip files in /mjpeg (.mjp, .mjpg, .mjpeg), sorted by name.
  std::vector<std::string> list_mjpegs();

  /// Playback rate for clips. A file's own fps is ignored: the server
  /// resamples to this rate, and a hand-copied file simply plays at it.
  void set_fps(float fps) { this->fps_ = fps; }
  void set_playing(bool playing);
  bool is_playing() const { return this->playing_; }

  /// Key of the item on the panel ("file:<name>" for a hand-copied file), or
  /// empty.
  std::string shown_key() const { return this->shown_ != nullptr ? this->shown_->key : ""; }
  int shown_frames() const { return this->shown_ != nullptr ? (int) this->shown_->off.size() : 0; }
  size_t shown_bytes() const { return this->shown_ != nullptr ? this->shown_->len : 0; }
  size_t cache_count() const { return this->cache_.size(); }
  size_t cache_used() const;
  /// Frames decoded since boot; the caller diffs it for a rate.
  uint32_t frames_shown() const { return this->frames_shown_; }
  float last_decode_ms() const { return this->last_decode_us_ / 1000.0f; }
  float last_load_ms() const { return this->last_load_ms_; }

  /// Largest single item held in memory; longer clips are cut to whole frames.
  void set_max_bytes(size_t max_bytes) { this->max_bytes_ = max_bytes; }
  size_t max_bytes() const { return this->max_bytes_; }
  /// PSRAM for all items together.
  void set_cache_bytes(size_t cache_bytes) { this->cache_bytes_ = cache_bytes; }

 protected:
  bool ensure_buffer_();
  /// Banded read-and-blit of one raw frame file. Shared by clips and stills.
  bool blit_file_(const std::string &full, const char *what);
  std::string still_path_(const char *name) const;

  /// One HTTP body streamed to a card file or a PSRAM buffer, a bounded amount
  /// per loop() tick. Raw stills and MJPEG items both use it.
  enum FetchKind { FETCH_STILL, FETCH_ITEM };
  struct Fetch {
    std::shared_ptr<http_request::HttpContainer> http;
    FetchKind kind{FETCH_STILL};
    std::string key;         ///< item key (FETCH_ITEM)
    FILE *file{nullptr};     ///< card target, or nullptr when streaming to memory
    uint8_t *mem{nullptr};   ///< memory target (PSRAM), owned until finished
    std::string dir;         ///< absolute directory, e.g. "/sd/still"
    std::string live;        ///< final file name inside dir
    std::string etag_file;   ///< file inside dir that holds the ETag, or empty
    std::string etag;
    size_t expected{0};
    size_t written{0};
    uint32_t last_data{0};
    uint32_t started{0};
  };
  bool start_fetch_(FetchKind kind, const std::string &key, const std::string &url,
                    const std::string &dir, const std::string &live, const std::string &etag_file,
                    bool offer_etag, bool to_memory);
  void pump_fetch_();
  void finish_fetch_(bool ok);
  /// Drop a download without reporting anything, e.g. when the screen has
  /// been switched to something else meanwhile.
  void abort_fetch_();
  void fetch_failed_(FetchKind kind, const std::string &key, bool unchanged = false);

  /// An item in PSRAM.
  struct Item {
    std::string key;
    uint8_t *buf{nullptr};
    size_t len{0};
    std::vector<uint32_t> off;
    std::vector<uint32_t> size;
    uint32_t used{0};  ///< millis() of last show, for LRU eviction
    ~Item();
  };
  Item *find_item_(const std::string &key);
  /// Make room for `len` more bytes in memory and allocate them. Evicts least
  /// recently used items first, and the item on the panel only as a last
  /// resort. nullptr if even an empty cache cannot fit it.
  uint8_t *reserve_(size_t len);
  void evict_(Item *item);
  bool start_load_(const std::string &key, const std::string &path);
  void pump_load_();
  void abort_load_();
  /// Index `buf` into a cached item; takes ownership of `buf` either way.
  void adopt_(const std::string &key, uint8_t *buf, size_t len, uint32_t started);
  void activate_(Item *item);
  void update_high_freq_();
  void pump_playback_();
  bool show_frame_(size_t index);
  static int jpeg_draw_(jpeg_draw_tag *draw);
  /// Card path of cached item `key`, relative to the mount point.
  std::string cache_path_(const std::string &key) const;
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

  Fetch fetch_;
  std::unique_ptr<uint8_t[]> fetch_chunk_;
  int still_event_{STILL_NONE};

  // -- item state --
  struct Load {
    int fd{-1};
    uint8_t *buf{nullptr};
    size_t cap{0};
    size_t got{0};
    std::string key;
    std::string path;
    uint32_t started{0};
  } load_;

  size_t max_bytes_{4000000};
  size_t cache_bytes_{5000000};
  std::vector<std::unique_ptr<Item>> cache_;
  Item *shown_{nullptr};
  /// The item most recently asked for. Work that finishes for any other key
  /// is cached but not shown.
  std::string want_key_;

  JPEGDEC *jpeg_{nullptr};
  /// Where the decoded image's top-left lands on the panel. Negative when the
  /// item is larger than the panel: it is centre-cropped in jpeg_draw_().
  int draw_ox_{0};
  int draw_oy_{0};

  float fps_{15.0f};
  bool playing_{true};
  size_t play_index_{0};
  uint32_t next_due_{0};
  uint32_t frames_shown_{0};
  uint32_t last_decode_us_{0};
  float last_load_ms_{0};
  uint32_t decode_errors_{0};
  int item_event_{ITEM_NONE};
  /// Playback deadlines are tens of ms apart; ESPHome's default ~16 ms loop
  /// cadence would add that much jitter to every frame.
  HighFrequencyLoopRequester high_freq_;
};

}  // namespace sd_clip
}  // namespace esphome

#endif  // USE_ESP32
