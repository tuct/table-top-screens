#include "sd_clip.h"

#ifdef USE_ESP32

#include <cstdio>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "esp_heap_caps.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <new>
#include <vector>

#include <JPEGDEC.h>

#include "esphome/core/log.h"

namespace esphome {
namespace sd_clip {

static const char *const TAG = "sd_clip";

void SdClip::setup() {
  if (this->sd_ == nullptr || !this->sd_->is_mounted()) {
    ESP_LOGW(TAG, "No SD card mounted; clip playback is unavailable.");
    return;
  }
  // Create the clip directory up front so a later cache() only has to worry
  // about files. mkdir on an existing directory fails harmlessly with EEXIST.
  const std::string dir = this->sd_->mount_point() + this->directory_;
  ::mkdir(dir.c_str(), 0777);

  // Count what a previous run already left on the card, so playback can start
  // immediately after a reboot with no network at all.
  while (this->sd_->file_size(this->frame_path_(this->cached_)) == this->frame_bytes()) {
    this->cached_++;
  }
  if (this->cached_ > 0) {
    ESP_LOGI(TAG, "%d frames already cached on the card", this->cached_);
  }
}

void SdClip::dump_config() {
  ESP_LOGCONFIG(TAG, "SD clip player:");
  ESP_LOGCONFIG(TAG, "  Frame size: %dx%d (%u bytes each)", this->width_, this->height_,
                static_cast<unsigned>(this->frame_bytes()));
  ESP_LOGCONFIG(TAG, "  Directory: %s", this->directory_.c_str());
  ESP_LOGCONFIG(TAG, "  Frames cached: %d", this->cached_);
}

std::string SdClip::frame_path_(int index) const {
  char name[32];
  snprintf(name, sizeof(name), "/%04d.565", index);
  return this->directory_ + name;
}

std::string SdClip::full_path_(int index) const {
  return this->sd_->mount_point() + this->frame_path_(index);
}

bool SdClip::ensure_buffer_() {
  if (this->buffer_ != nullptr)
    return true;

  const size_t row_bytes = static_cast<size_t>(this->width_) * 2;
  // A band must be a whole number of 512-byte sectors, or FATFS drags the read
  // back through its own sector window and the DMA path is wasted. The
  // smallest row count that satisfies that is 512/gcd(row_bytes, 512): 8 rows
  // at 800 wide, 16 at 240.
  size_t g = row_bytes, b = 512;
  while (b != 0) {
    const size_t t = g % b;
    g = b;
    b = t;
  }
  const int step = static_cast<int>(512 / g);

  // ~48 KB: comfortably inside internal RAM while still being a big enough
  // transfer that per-command overhead stops mattering.
  const int want = static_cast<int>(49152 / row_bytes);
  this->band_rows_ = (want / step) * step;
  if (this->band_rows_ < step)
    this->band_rows_ = step;
  if (this->band_rows_ > this->height_)
    this->band_rows_ = this->height_;

  // INTERNAL, not PSRAM: this is the entire point. See show().
  const size_t bytes = static_cast<size_t>(this->band_rows_) * row_bytes;
  this->buffer_ = static_cast<uint8_t *>(
      heap_caps_malloc(bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
  if (this->buffer_ == nullptr) {
    ESP_LOGE(TAG, "Cannot allocate a %u byte band buffer in internal RAM",
             static_cast<unsigned>(bytes));
    this->band_rows_ = 0;
    return false;
  }
  ESP_LOGI(TAG, "Band buffer: %d rows, %u bytes (internal, DMA-capable)", this->band_rows_,
           static_cast<unsigned>(bytes));
  return true;
}

bool SdClip::has_frame(int index) {
  return this->sd_ != nullptr && this->sd_->is_mounted() &&
         this->sd_->file_size(this->frame_path_(index)) == this->frame_bytes();
}

int SdClip::rescan() {
  int n = 0;
  while (this->has_frame(n))
    n++;
  this->cached_ = n;
  return n;
}

bool SdClip::ensure_clip(const std::string &token) {
  if (this->sd_ == nullptr || !this->sd_->is_mounted())
    return false;

  const std::string id_path = this->sd_->mount_point() + this->directory_ + "/id.txt";
  std::string current;
  FILE *f = fopen(id_path.c_str(), "rb");
  if (f != nullptr) {
    char buf[256];
    const size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    current = buf;
  }
  if (current == token)
    return true;

  // Different clip: drop every frame. Leaving them would mean a new animation
  // of the same length plays the previous one's pixels.
  ESP_LOGI(TAG, "Clip changed; clearing cached frames");
  for (int i = 0; this->has_frame(i); i++) {
    ::remove(this->full_path_(i).c_str());
  }
  this->cached_ = 0;

  f = fopen(id_path.c_str(), "wb");
  if (f != nullptr) {
    fwrite(token.c_str(), 1, token.size(), f);
    fclose(f);
  } else {
    ESP_LOGW(TAG, "Could not record the clip id; frames may be re-cached needlessly");
  }
  return false;
}

bool SdClip::write_frame(int index, const uint8_t *data, size_t len) {
  // Say which precondition failed. This used to return silently, which made a
  // simple "no card mounted" look like a write bug.
  if (this->sd_ == nullptr || !this->sd_->is_mounted()) {
    ESP_LOGW(TAG, "Frame %d not written: no SD card mounted", index);
    return false;
  }
  if (data == nullptr) {
    ESP_LOGW(TAG, "Frame %d not written: decoder produced no buffer", index);
    return false;
  }
  if (len != this->frame_bytes()) {
    ESP_LOGE(TAG, "Frame %d: decoder gave %u bytes, expected %u", index,
             static_cast<unsigned>(len), static_cast<unsigned>(this->frame_bytes()));
    return false;
  }

  const std::string full = this->full_path_(index);
  FILE *f = fopen(full.c_str(), "wb");
  if (f == nullptr) {
    ESP_LOGE(TAG, "Frame %d: cannot create %s", index, full.c_str());
    return false;
  }

  // One call for the whole frame. The buffer is already contiguous, and FATFS
  // turns a single large write into full-cluster writes, where a stream of
  // small ones costs a read-modify-write per partial cluster.
  const uint32_t t0 = micros();
  const size_t wrote = fwrite(data, 1, len, f);
  fclose(f);
  this->last_write_us_ = micros() - t0;

  if (wrote != len) {
    ESP_LOGE(TAG, "Frame %d: short write (%u of %u); removing", index,
             static_cast<unsigned>(wrote), static_cast<unsigned>(len));
    ::remove(full.c_str());
    return false;
  }

  if (index >= this->cached_)
    this->cached_ = index + 1;
  ESP_LOGD(TAG, "Frame %d written in %u ms (%u KB/s)", index,
           static_cast<unsigned>(this->last_write_us_ / 1000),
           static_cast<unsigned>(this->last_write_us_ > 0
                                     ? (static_cast<uint64_t>(len) * 1000000ULL /
                                        this->last_write_us_ / 1024ULL)
                                     : 0));
  return true;
}

bool SdClip::cache_one(const std::string &base_url, int index) {
  if (this->sd_ == nullptr || !this->sd_->is_mounted()) {
    ESP_LOGE(TAG, "Cannot cache: no SD card mounted.");
    return false;
  }
  if (this->http_ == nullptr) {
    ESP_LOGE(TAG, "Cannot cache: no http_request component configured.");
    return false;
  }
  if (this->has_frame(index)) {
    if (index >= this->cached_)
      this->cached_ = index + 1;
    return true;
  }

  const size_t expected = this->frame_bytes();
  const std::string url = base_url + "&n=" + std::to_string(index);
  auto container = this->http_->get(url);
  if (container == nullptr) {
    ESP_LOGE(TAG, "Frame %d: request failed", index);
    return false;
  }
  if (container->status_code != 200) {
    ESP_LOGE(TAG, "Frame %d: HTTP %d", index, container->status_code);
    container->end();
    return false;
  }
  if (container->content_length != expected) {
    // A size mismatch means the server rendered for a different panel. Blitting
    // it would read past the end of the buffer, so refuse rather than store a
    // frame that cannot be played safely.
    ESP_LOGE(TAG, "Frame %d: expected %u bytes, server sent %u", index,
             static_cast<unsigned>(expected), static_cast<unsigned>(container->content_length));
    container->end();
    return false;
  }

  // Chunked straight to the card: never hold a whole frame in RAM just to
  // write it, so this works even while the playback buffer is in use.
  static const size_t CHUNK = 8192;
  std::unique_ptr<uint8_t[]> chunk(new uint8_t[CHUNK]);
  const std::string full = this->full_path_(index);
  FILE *f = fopen(full.c_str(), "wb");
  if (chunk == nullptr || f == nullptr) {
    ESP_LOGE(TAG, "Frame %d: cannot create %s", index, full.c_str());
    if (f != nullptr)
      fclose(f);
    container->end();
    return false;
  }

  size_t written = 0;
  bool ok = true;
  uint32_t http_us = 0, write_us = 0;
  while (written < expected) {
    const uint32_t th = micros();
    const int got = container->read(chunk.get(), CHUNK);
    http_us += micros() - th;
    if (got <= 0)
      break;
    const uint32_t tw = micros();
    if (fwrite(chunk.get(), 1, static_cast<size_t>(got), f) != static_cast<size_t>(got)) {
      ESP_LOGE(TAG, "Frame %d: write failed -- card full?", index);
      ok = false;
      break;
    }
    write_us += micros() - tw;
    written += static_cast<size_t>(got);
    // Even a single 768 KB frame is far longer than a loop tick.
    App.feed_wdt();
  }
  fclose(f);
  container->end();

  if (!ok || written != expected) {
    // A half-written frame would be read back as a torn image, and worse,
    // file_size() would not flag it if playback used a looser check. Remove it
    // so the next pass retries cleanly.
    ESP_LOGE(TAG, "Frame %d: short write (%u of %u); removing", index,
             static_cast<unsigned>(written), static_cast<unsigned>(expected));
    ::remove(full.c_str());
    return false;
  }

  this->last_http_us_ = http_us;
  this->last_write_us_ = write_us;
  // The number that decides what to fix: if HTTP dominates, compress the
  // transport; if the write dominates, compression cannot help at all,
  // because the same 768 KB still lands on the card either way.
  ESP_LOGI(TAG, "Frame %d: %u ms HTTP + %u ms card write", index,
           static_cast<unsigned>(http_us / 1000), static_cast<unsigned>(write_us / 1000));

  if (index >= this->cached_)
    this->cached_ = index + 1;
  return true;
}

bool SdClip::show(int index) {
  char what[16];
  snprintf(what, sizeof(what), "Frame %d", index);
  return this->blit_file_(this->full_path_(index), what);
}

bool SdClip::blit_file_(const std::string &full, const char *what) {
  if (this->display_ == nullptr || this->sd_ == nullptr || !this->sd_->is_mounted())
    return false;
  if (!this->ensure_buffer_())
    return false;

  // POSIX rather than stdio: fread would add another layer of buffering
  // between the card and a buffer that is deliberately DMA-capable.
  const int fd = ::open(full.c_str(), O_RDONLY);
  if (fd < 0) {
    ESP_LOGW(TAG, "%s: cannot open %s", what, full.c_str());
    return false;
  }

  const size_t row_bytes = static_cast<size_t>(this->width_) * 2;
  uint32_t read_us = 0, blit_us = 0;
  bool ok = true;

  for (int y = 0; y < this->height_; y += this->band_rows_) {
    const int rows = std::min(this->band_rows_, this->height_ - y);
    const size_t want = static_cast<size_t>(rows) * row_bytes;

    const uint32_t t0 = micros();
    size_t got = 0;
    while (got < want) {
      const ssize_t n = ::read(fd, this->buffer_ + got, want - got);
      if (n <= 0)
        break;
      got += static_cast<size_t>(n);
    }
    read_us += micros() - t0;
    if (got != want) {
      ESP_LOGW(TAG, "%s: short read at row %d (%u of %u)", what, y,
               static_cast<unsigned>(got), static_cast<unsigned>(want));
      ok = false;
      break;
    }

    // Blit the band straight to its row range. Because each band goes out as
    // it arrives, no full-frame buffer is ever needed.
    const uint32_t t1 = micros();
    this->display_->draw_pixels_at(0, y, this->width_, rows, this->buffer_,
                                   display::COLOR_ORDER_RGB, display::COLOR_BITNESS_565, true, 0, 0,
                                   0);
    blit_us += micros() - t1;
  }

  ::close(fd);
  if (!ok)
    return false;

  this->last_read_us_ = read_us;
  this->last_blit_us_ = blit_us;
  return true;
}

// ---------------------------------------------------------------------------
// downloads (raw stills and MJPEG items)
// ---------------------------------------------------------------------------

// Per loop() tick. Big enough that a 768 KB still takes a dozen ticks rather
// than hundreds, small enough that WiFi and web_server still get a look in.
static const size_t FETCH_BYTES_PER_TICK = 64 * 1024;
static const size_t FETCH_CHUNK = 8192;
// No data for this long mid-transfer means the server or the link is gone.
static const uint32_t FETCH_STALL_MS = 10000;
// Written next to the live file and renamed over it only once complete, so
// the old file stays intact and usable until then. Not a clip extension, so
// list_mjpegs() never offers a half-downloaded file.
static const char *const FETCH_TEMP = "new.part";
// Keep this much of the card free. Past it, items are still downloaded and
// shown, but held in memory only.
static const uint64_t CARD_RESERVE = 16ULL * 1024 * 1024;

std::string SdClip::still_path_(const char *name) const {
  return this->sd_->mount_point() + "/still/" + name;
}

bool SdClip::has_still() {
  return this->sd_ != nullptr && this->sd_->is_mounted() &&
         this->sd_->file_size("/still/image.565") == this->frame_bytes();
}

bool SdClip::start_still(const std::string &url) {
  if (this->sd_ == nullptr || !this->sd_->is_mounted()) {
    this->still_event_ = STILL_FAILED;
    return false;
  }
  // Only offer our ETag if the file it describes is really there and whole;
  // otherwise a 304 would leave us with nothing to show.
  return this->start_fetch_(FETCH_STILL, "", url, this->sd_->mount_point() + "/still", "image.565",
                            "etag.txt", this->has_still(), false);
}

bool SdClip::start_fetch_(FetchKind kind, const std::string &key, const std::string &url,
                          const std::string &dir, const std::string &live,
                          const std::string &etag_file, bool offer_etag, bool to_memory) {
  if (this->still_busy())
    return false;
  if (this->http_ == nullptr) {
    this->fetch_failed_(kind, key);
    return false;
  }
  if (!to_memory)
    ::mkdir(dir.c_str(), 0777);

  std::vector<http_request::Header> headers;
  if (offer_etag && !etag_file.empty()) {
    FILE *f = fopen((dir + "/" + etag_file).c_str(), "rb");
    if (f != nullptr) {
      char buf[128];
      const size_t n = fread(buf, 1, sizeof(buf) - 1, f);
      fclose(f);
      buf[n] = '\0';
      if (n > 0)
        headers.push_back({"If-None-Match", buf});
    }
  }
  const char *what = kind == FETCH_STILL ? "Still" : "Item";

  const uint32_t started = millis();
  auto container = this->http_->get(url, headers, {"etag"});
  if (container == nullptr) {
    ESP_LOGW(TAG, "%s: request failed", what);
    this->fetch_failed_(kind, key);
    return false;
  }
  if (container->status_code == 304 && kind == FETCH_STILL) {
    container->end();
    ESP_LOGD(TAG, "%s: unchanged (304)", what);
    this->fetch_failed_(kind, key, true);
    return true;
  }
  if (container->status_code != 200) {
    ESP_LOGW(TAG, "%s: HTTP %d", what, container->status_code);
    container->end();
    this->fetch_failed_(kind, key);
    return false;
  }

  const size_t len = container->content_length;
  // A still rendered for a different panel, or not raw at all, would read
  // past the end of a row when blitted. An item must fit memory whole.
  const bool size_ok = kind == FETCH_STILL ? len == this->frame_bytes()
                                           : len > 0 && len <= this->max_bytes_;
  if (!size_ok) {
    ESP_LOGW(TAG, "%s: unusable size %u bytes", what, static_cast<unsigned>(len));
    container->end();
    this->fetch_failed_(kind, key);
    return false;
  }

  // On the heap: the loop task's stack is only ~8 KB, so a chunk this size
  // cannot live in loop()'s frame.
  if (this->fetch_chunk_ == nullptr)
    this->fetch_chunk_.reset(new (std::nothrow) uint8_t[FETCH_CHUNK]);
  if (this->fetch_chunk_ == nullptr) {
    ESP_LOGW(TAG, "%s: cannot allocate a %u byte chunk", what, static_cast<unsigned>(FETCH_CHUNK));
    container->end();
    this->fetch_failed_(kind, key);
    return false;
  }

  Fetch fetch;
  fetch.kind = kind;
  fetch.key = key;
  fetch.dir = dir;
  fetch.live = live;
  fetch.etag_file = etag_file;
  fetch.expected = len;
  if (to_memory) {
    fetch.mem = this->reserve_(len);
    if (fetch.mem == nullptr) {
      ESP_LOGW(TAG, "%s: cannot allocate %u KB of PSRAM", what, static_cast<unsigned>(len / 1024));
      container->end();
      this->fetch_failed_(kind, key);
      return false;
    }
  } else {
    const std::string temp = dir + "/" + FETCH_TEMP;
    fetch.file = fopen(temp.c_str(), "wb");
    if (fetch.file == nullptr) {
      ESP_LOGW(TAG, "%s: cannot create %s", what, temp.c_str());
      container->end();
      this->fetch_failed_(kind, key);
      return false;
    }
  }
  fetch.etag = container->get_response_header("etag");
  fetch.started = started;
  fetch.last_data = millis();
  fetch.http = container;
  this->fetch_ = std::move(fetch);
  return true;
}

void SdClip::pump_fetch_() {
  Fetch &fx = this->fetch_;
  uint8_t *chunk = this->fetch_chunk_.get();
  size_t budget = FETCH_BYTES_PER_TICK;
  while (budget > 0 && fx.written < fx.expected) {
    const size_t want = std::min(FETCH_CHUNK, fx.expected - fx.written);
    // Straight into the PSRAM buffer when there is one: no copy needed.
    uint8_t *dst = fx.mem != nullptr ? fx.mem + fx.written : chunk;
    const int got = fx.http->read(dst, want);
    auto r = http_request::http_read_loop_result(got, fx.last_data, FETCH_STALL_MS,
                                                 fx.http->is_read_complete());
    if (r == http_request::HttpReadLoopResult::RETRY)
      return;  // nothing yet; come back next tick
    if (r != http_request::HttpReadLoopResult::DATA)
      break;
    if (fx.file != nullptr &&
        fwrite(chunk, 1, static_cast<size_t>(got), fx.file) != static_cast<size_t>(got)) {
      ESP_LOGW(TAG, "Download: write failed -- card full?");
      break;
    }
    fx.written += static_cast<size_t>(got);
    budget -= std::min(budget, static_cast<size_t>(got));
  }

  if (fx.written >= fx.expected) {
    this->finish_fetch_(true);
  } else if (budget > 0) {
    // Left the loop early without finishing: error, timeout or write failure.
    this->finish_fetch_(false);
  }
}

void SdClip::abort_fetch_() {
  Fetch fx = std::move(this->fetch_);
  this->fetch_ = Fetch{};
  if (fx.http == nullptr)
    return;
  ESP_LOGD(TAG, "Download of %s dropped", fx.key.c_str());
  if (fx.file != nullptr) {
    fclose(fx.file);
    ::remove((fx.dir + "/" + FETCH_TEMP).c_str());
  }
  if (fx.mem != nullptr)
    heap_caps_free(fx.mem);
  fx.http->end();
}

void SdClip::finish_fetch_(bool ok) {
  // Move the state out first: what happens next may start the next step (a
  // load), and must see the download slot as free.
  Fetch fx = std::move(this->fetch_);
  this->fetch_ = Fetch{};
  if (fx.file != nullptr)
    fclose(fx.file);
  fx.http->end();
  fx.http.reset();
  const char *what = fx.kind == FETCH_STILL ? "Still" : "Item";

  if (!ok) {
    ESP_LOGW(TAG, "%s: transfer stopped at %u of %u bytes", what,
             static_cast<unsigned>(fx.written), static_cast<unsigned>(fx.expected));
    if (fx.mem != nullptr)
      heap_caps_free(fx.mem);
    else
      ::remove((fx.dir + "/" + FETCH_TEMP).c_str());
    this->fetch_failed_(fx.kind, fx.key);
    return;
  }

  ESP_LOGI(TAG, "%s %s: %u KB received in %u ms", what, fx.key.c_str(),
           static_cast<unsigned>(fx.written / 1024), static_cast<unsigned>(millis() - fx.started));

  if (fx.mem != nullptr) {
    // Streamed straight into PSRAM: this buffer IS the item.
    this->adopt_(fx.key, fx.mem, fx.written, fx.started);
    return;
  }

  // FATFS will not rename over an existing file, so the old one goes first.
  const std::string fresh = fx.dir + "/" + FETCH_TEMP;
  const std::string live = fx.dir + "/" + fx.live;
  ::remove(live.c_str());
  if (::rename(fresh.c_str(), live.c_str()) != 0) {
    ESP_LOGW(TAG, "%s: cannot move %s into place", what, fresh.c_str());
    ::remove(fresh.c_str());
    this->fetch_failed_(fx.kind, fx.key);
    return;
  }
  if (!fx.etag_file.empty()) {
    FILE *f = fopen((fx.dir + "/" + fx.etag_file).c_str(), "wb");
    if (f != nullptr) {
      fwrite(fx.etag.data(), 1, fx.etag.size(), f);
      fclose(f);
    }
  }
  if (fx.kind == FETCH_STILL) {
    this->still_event_ = STILL_UPDATED;
    return;
  }
  // On the card now; into memory from there.
  this->revision_++;
  if (!this->start_load_(fx.key, this->cache_path_(fx.key)) && fx.key == this->want_key_)
    this->item_event_ = ITEM_FAILED;
}

void SdClip::fetch_failed_(FetchKind kind, const std::string &key, bool unchanged) {
  if (kind == FETCH_STILL) {
    this->still_event_ = unchanged ? STILL_UNCHANGED : STILL_FAILED;
  } else if (key == this->want_key_) {
    this->item_event_ = ITEM_FAILED;
  }
}

void SdClip::loop() {
  if (this->fetch_.http != nullptr)
    this->pump_fetch_();
  if (this->load_.fd >= 0)
    this->pump_load_();
  this->pump_playback_();
}

bool SdClip::show_still() {
  if (!this->has_still())
    return false;
  return this->blit_file_(this->still_path_("image.565"), "Still");
}

// ---------------------------------------------------------------------------
// MJPEG items: requests and the cache
// ---------------------------------------------------------------------------

// Card-to-PSRAM copy budget per tick. The copy goes through the internal DMA
// band buffer (see show()), so each read is one band.
static const size_t LOAD_BYTES_PER_TICK = 64 * 1024;
static const char *const FILE_PREFIX = "file:";

SdClip::Item::~Item() {
  if (this->buf != nullptr)
    heap_caps_free(this->buf);
}

std::string SdClip::cache_path_(const std::string &key) const {
  // Keys come from a URL; keep a filename FAT accepts on every card.
  std::string name;
  for (char c : key) {
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                    c == '.' || c == '-' || c == '_';
    name += ok ? c : '_';
    if (name.size() >= 96)
      break;
  }
  return "/mjpeg/cache/" + name + ".mjp";
}

size_t SdClip::cache_used() const {
  size_t used = 0;
  for (auto &it : this->cache_)
    used += it->len;
  return used;
}

SdClip::Item *SdClip::find_item_(const std::string &key) {
  for (auto &it : this->cache_) {
    if (it->key == key)
      return it.get();
  }
  return nullptr;
}

bool SdClip::has_item(const std::string &key) {
  if (this->find_item_(key) != nullptr)
    return true;
  return this->sd_ != nullptr && this->sd_->is_mounted() &&
         this->sd_->file_size(this->cache_path_(key)) > 0;
}

bool SdClip::request_item(const std::string &key, const std::string &url) {
  this->want_key_ = key;
  if (this->shown_ != nullptr && this->shown_->key == key) {
    this->shown_->used = millis();
    this->item_event_ = ITEM_SHOWN;
    return true;
  }
  if (Item *it = this->find_item_(key)) {
    this->activate_(it);
    return true;
  }
  // Whatever was underway was for something the screen no longer wants.
  if (this->load_.fd >= 0 && this->load_.key != key)
    this->abort_load_();
  if (this->fetch_.http != nullptr && this->fetch_.kind == FETCH_ITEM && this->fetch_.key != key)
    this->abort_fetch_();
  if (this->load_.fd >= 0 || (this->fetch_.http != nullptr && this->fetch_.key == key))
    return true;  // already on its way

  const bool card = this->sd_ != nullptr && this->sd_->is_mounted();
  if (card && this->sd_->file_size(this->cache_path_(key)) > 0) {
    ESP_LOGI(TAG, "Item %s: on the card, no download", key.c_str());
    if (this->start_load_(key, this->cache_path_(key)))
      return true;
    // Unreadable: fall through and fetch it again.
  }
  if (url.empty() || this->still_busy())
    return false;

  bool to_card = card;
  uint64_t total, free;
  if (to_card && this->sd_->space(total, free) && free < CARD_RESERVE + this->max_bytes_) {
    ESP_LOGW(TAG, "Card nearly full (%u MB free); holding %s in memory only",
             static_cast<unsigned>(free >> 20), key.c_str());
    to_card = false;
  }
  if (to_card) {
    ::mkdir((this->sd_->mount_point() + "/mjpeg").c_str(), 0777);
    const std::string path = this->cache_path_(key);
    return this->start_fetch_(FETCH_ITEM, key, url, this->sd_->mount_point() + "/mjpeg/cache",
                              path.substr(path.rfind('/') + 1), "", false, false);
  }
  return this->start_fetch_(FETCH_ITEM, key, url, "", "", "", false, true);
}

bool SdClip::request_file(const std::string &name) {
  const std::string key = FILE_PREFIX + name;
  this->want_key_ = key;
  if (this->shown_ != nullptr && this->shown_->key == key) {
    this->item_event_ = ITEM_SHOWN;
    return true;
  }
  if (Item *it = this->find_item_(key)) {
    this->activate_(it);
    return true;
  }
  if (this->load_.fd >= 0) {
    if (this->load_.key == key)
      return true;
    this->abort_load_();
  }
  if (this->fetch_.http != nullptr && this->fetch_.kind == FETCH_ITEM)
    this->abort_fetch_();
  if (this->sd_ == nullptr || !this->sd_->is_mounted())
    return false;
  if (!this->start_load_(key, "/mjpeg/" + name)) {
    this->item_event_ = ITEM_FAILED;
    return true;  // a missing file will not appear by asking again
  }
  return true;
}

std::vector<std::string> SdClip::list_mjpegs() {
  std::vector<std::string> out;
  if (this->sd_ == nullptr || !this->sd_->is_mounted())
    return out;
  for (auto &name : this->sd_->list_directory("/mjpeg", 64)) {
    std::string lower = name;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
    auto ends_with = [&](const char *ext) {
      const size_t n = strlen(ext);
      return lower.size() > n && lower.compare(lower.size() - n, n, ext) == 0;
    };
    if (ends_with(".mjp") || ends_with(".mjpg") || ends_with(".mjpeg"))
      out.push_back(name);
  }
  std::sort(out.begin(), out.end());
  return out;
}

void SdClip::evict_(Item *item) {
  if (item == this->shown_) {
    // The panel keeps the last pixels; asking for this key again reloads it
    // from the card (or the network) rather than finding it here.
    this->shown_ = nullptr;
    this->update_high_freq_();
  }
  ESP_LOGD(TAG, "Evicting %s (%u KB) from memory", item->key.c_str(),
           static_cast<unsigned>(item->len / 1024));
  this->revision_++;
  this->cache_.erase(std::remove_if(this->cache_.begin(), this->cache_.end(),
                                    [item](const std::unique_ptr<Item> &p) { return p.get() == item; }),
                     this->cache_.end());
}

uint8_t *SdClip::reserve_(size_t len) {
  auto oldest = [this](bool spare_shown) -> Item * {
    Item *best = nullptr;
    for (auto &it : this->cache_) {
      if (spare_shown && it.get() == this->shown_)
        continue;
      if (best == nullptr || it->used < best->used)
        best = it.get();
    }
    return best;
  };
  size_t used = this->cache_used();
  while (used + len > this->cache_bytes_) {
    Item *victim = oldest(true);
    if (victim == nullptr)
      victim = oldest(false);
    if (victim == nullptr)
      break;
    used -= victim->len;
    this->evict_(victim);
  }
  auto *buf = static_cast<uint8_t *>(heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  // Fragmentation, or PSRAM spent elsewhere: give up the whole cache and try
  // once more before failing.
  while (buf == nullptr && !this->cache_.empty()) {
    Item *victim = oldest(true);
    if (victim == nullptr)
      victim = oldest(false);
    this->evict_(victim);
    buf = static_cast<uint8_t *>(heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  }
  return buf;
}

bool SdClip::start_load_(const std::string &key, const std::string &path) {
  if (!this->ensure_buffer_())
    return false;
  this->abort_load_();

  const size_t size = this->sd_->file_size(path);
  if (size == 0) {
    ESP_LOGW(TAG, "%s: missing or empty", path.c_str());
    return false;
  }
  // At most max_bytes. Frames past the cut are dropped whole when the item is
  // indexed, so an oversized file still plays -- just shorter.
  const size_t cap = std::min(size, this->max_bytes_);
  if (size > cap) {
    ESP_LOGW(TAG, "%s is %u KB; loading the first %u KB", path.c_str(),
             static_cast<unsigned>(size / 1024), static_cast<unsigned>(cap / 1024));
  }
  uint8_t *buf = this->reserve_(cap);
  if (buf == nullptr) {
    ESP_LOGW(TAG, "%s: cannot allocate %u KB of PSRAM", path.c_str(),
             static_cast<unsigned>(cap / 1024));
    return false;
  }
  const std::string full = this->sd_->mount_point() + path;
  const int fd = ::open(full.c_str(), O_RDONLY);
  if (fd < 0) {
    ESP_LOGW(TAG, "Cannot open %s", full.c_str());
    heap_caps_free(buf);
    return false;
  }
  this->load_.fd = fd;
  this->load_.buf = buf;
  this->load_.cap = cap;
  this->load_.got = 0;
  this->load_.key = key;
  this->load_.path = path;
  this->load_.started = millis();
  return true;
}

void SdClip::abort_load_() {
  if (this->load_.fd >= 0)
    ::close(this->load_.fd);
  if (this->load_.buf != nullptr)
    heap_caps_free(this->load_.buf);
  this->load_ = Load{};
}

void SdClip::pump_load_() {
  Load &ld = this->load_;
  const size_t band = static_cast<size_t>(this->band_rows_) * this->width_ * 2;
  size_t budget = LOAD_BYTES_PER_TICK;
  bool eof = false;
  while (budget > 0 && ld.got < ld.cap) {
    // Into the internal DMA band buffer, then memcpy to PSRAM: reading
    // straight into PSRAM drops the SD driver off its DMA path (see show()).
    const size_t want = std::min(band, ld.cap - ld.got);
    const ssize_t n = ::read(ld.fd, this->buffer_, want);
    if (n <= 0) {
      eof = true;
      break;
    }
    memcpy(ld.buf + ld.got, this->buffer_, static_cast<size_t>(n));
    ld.got += static_cast<size_t>(n);
    budget -= std::min(budget, static_cast<size_t>(n));
  }
  if (ld.got < ld.cap && !eof)
    return;  // more next tick

  ::close(ld.fd);
  uint8_t *buf = ld.buf;
  const size_t got = ld.got;
  const std::string key = ld.key;
  const uint32_t started = ld.started;
  this->load_ = Load{};
  this->adopt_(key, buf, got, started);
}

static void json_string(std::string &out, const std::string &value) {
  out += '"';
  for (char c : value) {
    if (c == '"' || c == '\\') {
      out += '\\';
      out += c;
    } else if (static_cast<unsigned char>(c) < 0x20) {
      out += '?';
    } else {
      out += c;
    }
  }
  out += '"';
}

std::string SdClip::cache_report(size_t max_card) {
  std::string out = str_sprintf(
      "{\"budget\":%u,\"used\":%u,\"max\":%u,\"psram_free\":%u,\"psram_total\":%u,"
      "\"heap_free\":%u,\"shown\":",
      static_cast<unsigned>(this->cache_bytes_), static_cast<unsigned>(this->cache_used()),
      static_cast<unsigned>(this->max_bytes_),
      static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)),
      static_cast<unsigned>(heap_caps_get_total_size(MALLOC_CAP_SPIRAM)),
      static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)));
  json_string(out, this->shown_key());
  out += ",\"mem\":[";
  bool first = true;
  for (auto &it : this->cache_) {
    if (!first)
      out += ',';
    first = false;
    out += '[';
    json_string(out, it->key);
    out += str_sprintf(",%u,%u]", static_cast<unsigned>(it->len), static_cast<unsigned>(it->off.size()));
  }
  out += "],\"card\":";
  if (this->sd_ == nullptr || !this->sd_->is_mounted()) {
    out += "null}";
    return out;
  }
  out += '[';
  first = true;
  size_t listed = 0;
  for (auto &name : this->sd_->list_directory("/mjpeg/cache", 128)) {
    if (name.size() <= 4 || name.compare(name.size() - 4, 4, ".mjp") != 0)
      continue;
    if (listed++ >= max_card)
      break;
    if (!first)
      out += ',';
    first = false;
    out += '[';
    json_string(out, name.substr(0, name.size() - 4));
    out += str_sprintf(",%u]", static_cast<unsigned>(this->sd_->file_size("/mjpeg/cache/" + name)));
  }
  out += "]}";
  return out;
}

bool SdClip::push_report(const std::string &url) {
  if (this->http_ == nullptr)
    return false;
  const std::string body = this->cache_report();
  const std::vector<http_request::Header> headers{{"Content-Type", "application/json"}};
  auto container = this->http_->post(url, body, headers);
  if (container == nullptr) {
    ESP_LOGD(TAG, "Report to %s failed", url.c_str());
    return false;
  }
  const int status = container->status_code;
  container->end();
  if (status < 200 || status >= 300) {
    ESP_LOGW(TAG, "Report to %s: HTTP %d", url.c_str(), status);
    return false;
  }
  ESP_LOGD(TAG, "Reported %u bytes of cache state", static_cast<unsigned>(body.size()));
  return true;
}

// ---------------------------------------------------------------------------
// MJPEG items: indexing
// ---------------------------------------------------------------------------

namespace {

uint16_t rd16le(const uint8_t *p) { return p[0] | (p[1] << 8); }
uint32_t rd32le(const uint8_t *p) { return p[0] | (p[1] << 8) | (p[2] << 16) | (static_cast<uint32_t>(p[3]) << 24); }
uint16_t rd16be(const uint8_t *p) { return (p[0] << 8) | p[1]; }

/// TTMJ container. Stops at the first record that runs past `len`, which is
/// how a clip truncated by max_bytes keeps its whole frames.
bool index_ttmj(const uint8_t *buf, size_t len, std::vector<uint32_t> &off,
                std::vector<uint32_t> &sizes, int &w, int &h, int &fps) {
  static const size_t HEADER = 16;
  if (len < HEADER || memcmp(buf, "TTMJ", 4) != 0)
    return false;
  if (rd16le(buf + 4) != 1) {
    ESP_LOGW(TAG, "TTMJ version %u is not supported", rd16le(buf + 4));
    return true;  // it IS a TTMJ file, just not one we can play
  }
  w = rd16le(buf + 6);
  h = rd16le(buf + 8);
  fps = rd16le(buf + 10);
  const uint32_t count = rd32le(buf + 12);
  size_t pos = HEADER;
  for (uint32_t i = 0; i < count; i++) {
    if (pos + 4 > len)
      break;
    const uint32_t n = rd32le(buf + pos);
    pos += 4;
    if (n == 0 || n > len - pos)
      break;
    off.push_back(pos);
    sizes.push_back(n);
    pos += n;
  }
  return true;
}

/// End of the JPEG starting at `soi` (one past its EOI), or 0 if it is cut off.
///
/// Walks marker segments by their lengths and only scans byte-by-byte inside
/// entropy-coded data. A bare FFD8/FFD9 search -- what video-Player does --
/// breaks on an embedded EXIF thumbnail, which carries its own SOI and EOI.
size_t jpeg_end(const uint8_t *buf, size_t len, size_t soi) {
  size_t p = soi + 2;
  // +2, not +4: a length-less marker such as the final EOI can sit in the
  // last two bytes of the buffer.
  while (p + 2 <= len) {
    if (buf[p] != 0xFF)
      return 0;
    const uint8_t m = buf[p + 1];
    if (m == 0xFF) {  // fill byte
      p++;
      continue;
    }
    if (m == 0xD9)
      return p + 2;
    if (m == 0x01 || (m >= 0xD0 && m <= 0xD7)) {  // no length field
      p += 2;
      continue;
    }
    if (p + 4 > len)
      return 0;
    const size_t seg = rd16be(buf + p + 2);
    p += 2 + seg;
    if (m != 0xDA)
      continue;
    // Entropy-coded data: FF00 is a stuffed byte and FFD0-D7 a restart
    // marker; any other marker ends the scan.
    while (p + 1 < len) {
      if (buf[p] != 0xFF) {
        p++;
        continue;
      }
      const uint8_t n = buf[p + 1];
      if (n == 0x00 || (n >= 0xD0 && n <= 0xD7)) {
        p += 2;
      } else if (n == 0xFF) {
        p++;
      } else {
        break;  // back to the marker walk (EOI, or another scan)
      }
    }
    if (p + 1 >= len)
      return 0;
  }
  return 0;
}

/// JPEGs back to back, optionally after video-Player's one-byte fps prefix.
bool index_raw(const uint8_t *buf, size_t len, std::vector<uint32_t> &off,
               std::vector<uint32_t> &sizes, int &fps) {
  size_t pos = 0;
  if (len >= 4 && buf[0] != 0xFF && buf[1] == 0xFF && buf[2] == 0xD8 && buf[3] == 0xFF) {
    fps = buf[0];
    pos = 1;
  }
  if (len < pos + 3 || buf[pos] != 0xFF || buf[pos + 1] != 0xD8)
    return false;
  while (pos + 3 < len) {
    if (!(buf[pos] == 0xFF && buf[pos + 1] == 0xD8 && buf[pos + 2] == 0xFF)) {
      pos++;  // padding between frames
      continue;
    }
    const size_t end = jpeg_end(buf, len, pos);
    if (end == 0)
      break;  // cut off by max_bytes (or corrupt): keep what came before
    off.push_back(pos);
    sizes.push_back(end - pos);
    pos = end;
  }
  return true;
}

}  // namespace

void SdClip::adopt_(const std::string &key, uint8_t *buf, size_t len, uint32_t started) {
  std::unique_ptr<Item> item(new Item());
  item->key = key;
  item->buf = buf;  // owned from here, freed with the item
  item->len = len;
  int w = 0, h = 0, fps = 0;
  const bool ttmj = index_ttmj(buf, len, item->off, item->size, w, h, fps);
  if (!ttmj && !index_raw(buf, len, item->off, item->size, fps))
    ESP_LOGW(TAG, "Item %s: neither TTMJ nor JPEG frames", key.c_str());
  if (item->off.empty()) {
    ESP_LOGW(TAG, "Item %s: no playable frames", key.c_str());
    if (key == this->want_key_)
      this->item_event_ = ITEM_FAILED;
    return;
  }

  this->last_load_ms_ = static_cast<float>(millis() - started);
  const size_t used = item->off.back() + item->size.back();
  ESP_LOGI(TAG, "Item %s: %s, %u frame(s), %u KB, file fps %d, ready in %u ms", key.c_str(),
           ttmj ? "TTMJ" : "raw MJPEG", static_cast<unsigned>(item->off.size()),
           static_cast<unsigned>(used / 1024), fps, static_cast<unsigned>(this->last_load_ms_));
  if (ttmj && (w != this->width_ || h != this->height_)) {
    ESP_LOGW(TAG, "Item %s was rendered at %dx%d for a %dx%d panel; it will be centred/cropped",
             key.c_str(), w, h, this->width_, this->height_);
  }

  // Replace an older copy under the same key rather than holding two.
  if (Item *old = this->find_item_(key))
    this->evict_(old);
  Item *raw = item.get();
  this->cache_.push_back(std::move(item));
  this->revision_++;
  ESP_LOGD(TAG, "Memory cache: %u item(s), %u KB", static_cast<unsigned>(this->cache_.size()),
           static_cast<unsigned>(this->cache_used() / 1024));
  if (key == this->want_key_)
    this->activate_(raw);
}

void SdClip::activate_(Item *item) {
  this->shown_ = item;
  this->revision_++;
  item->used = millis();
  this->decode_errors_ = 0;
  // The first frame goes up now, so a switch is visible immediately -- and a
  // still, which never plays, is drawn exactly once.
  this->show_frame_(0);
  this->play_index_ = item->off.size() > 1 ? 1 : 0;
  this->next_due_ = millis() + static_cast<uint32_t>(1000.0f / std::max(this->fps_, 1.0f));
  this->update_high_freq_();
  this->item_event_ = ITEM_SHOWN;
}

// ---------------------------------------------------------------------------
// MJPEG items: playback
// ---------------------------------------------------------------------------

void SdClip::set_playing(bool playing) {
  if (playing && !this->playing_)
    this->next_due_ = millis();
  this->playing_ = playing;
  this->update_high_freq_();
}

void SdClip::update_high_freq_() {
  // Only a playing clip needs the fast loop; a still costs nothing once drawn.
  if (this->playing_ && this->shown_ != nullptr && this->shown_->off.size() > 1)
    this->high_freq_.start();
  else
    this->high_freq_.stop();
}

void SdClip::pump_playback_() {
  if (!this->playing_ || this->shown_ == nullptr || this->shown_->off.size() < 2 || this->fps_ <= 0)
    return;
  const uint32_t now = millis();
  if (static_cast<int32_t>(now - this->next_due_) < 0)
    return;

  const uint32_t period = static_cast<uint32_t>(1000.0f / this->fps_);
  this->show_frame_(this->play_index_);
  this->play_index_ = (this->play_index_ + 1) % this->shown_->off.size();
  this->next_due_ += period;
  // More than a frame behind (a slow decode, a long blocking tick elsewhere):
  // resume from now instead of racing through frames to catch up.
  if (static_cast<int32_t>(now - this->next_due_) > static_cast<int32_t>(period))
    this->next_due_ = now + period;
}

bool SdClip::show_frame_(size_t index) {
  if (this->display_ == nullptr)
    return false;
  if (this->jpeg_ == nullptr) {
    // Internal RAM: the decoder's state holds its Huffman tables and MCU
    // scratch, and every one of them is touched per block.
    void *mem = heap_caps_malloc(sizeof(JPEGDEC), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (mem == nullptr)
      mem = heap_caps_malloc(sizeof(JPEGDEC), MALLOC_CAP_8BIT);
    if (mem == nullptr) {
      ESP_LOGE(TAG, "Cannot allocate the JPEG decoder (%u bytes)", static_cast<unsigned>(sizeof(JPEGDEC)));
      this->set_playing(false);
      return false;
    }
    this->jpeg_ = new (mem) JPEGDEC();
    ESP_LOGD(TAG, "JPEG decoder: %u bytes", static_cast<unsigned>(sizeof(JPEGDEC)));
  }

  const uint32_t t0 = micros();
  const Item *item = this->shown_;
  if (item == nullptr)
    return false;
  if (!this->jpeg_->openRAM(item->buf + item->off[index], static_cast<int>(item->size[index]),
                            &SdClip::jpeg_draw_)) {
    if (this->decode_errors_++ < 3)
      ESP_LOGW(TAG, "Frame %u: not a JPEG (error %d)", static_cast<unsigned>(index),
               this->jpeg_->getLastError());
    return false;
  }
  // After openRAM, which clears the decoder state.
  this->jpeg_->setUserPointer(this);
  this->jpeg_->setPixelType(RGB565_BIG_ENDIAN);
  this->draw_ox_ = (this->width_ - this->jpeg_->getWidth()) / 2;
  this->draw_oy_ = (this->height_ - this->jpeg_->getHeight()) / 2;
  const bool ok = this->jpeg_->decode(0, 0, 0) != 0;
  const int err = this->jpeg_->getLastError();
  this->jpeg_->close();
  this->last_decode_us_ = micros() - t0;
  if (!ok) {
    // Progressive JPEGs land here: JPEGDEC decodes baseline only.
    if (this->decode_errors_++ < 3)
      ESP_LOGW(TAG, "Frame %u: decode failed (error %d)", static_cast<unsigned>(index), err);
    return false;
  }
  this->frames_shown_++;
  return true;
}

int SdClip::jpeg_draw_(jpeg_draw_tag *draw) {
  auto *self = static_cast<SdClip *>(draw->pUser);
  // Block position on the panel. Clipped here rather than in the display
  // driver, which does not clip, so a clip larger than the panel -- a 320x180
  // video on a 240x240 screen -- is centre-cropped instead of scribbling past
  // the edge.
  const int dx = draw->x + self->draw_ox_;
  const int dy = draw->y + self->draw_oy_;
  const int skip_left = dx < 0 ? -dx : 0;
  const int skip_top = dy < 0 ? -dy : 0;
  const int x0 = dx + skip_left;
  const int y0 = dy + skip_top;
  const int w = std::min(draw->iWidthUsed - skip_left, self->width_ - x0);
  const int h = std::min(draw->iHeight - skip_top, self->height_ - y0);
  if (w <= 0 || h <= 0)
    return 1;  // entirely off the panel; keep decoding
  // Stride is iWidth; iWidthUsed excludes padding past the image's right edge.
  self->display_->draw_pixels_at(x0, y0, w, h, reinterpret_cast<const uint8_t *>(draw->pPixels),
                                 display::COLOR_ORDER_RGB, display::COLOR_BITNESS_565, true,
                                 skip_left, skip_top, draw->iWidth - skip_left - w);
  return 1;
}

}  // namespace sd_clip
}  // namespace esphome

#endif  // USE_ESP32
